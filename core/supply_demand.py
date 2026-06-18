#!/usr/bin/env python3
"""
供需评估模块（supply_demand.py）

从财务数据中提取6项硬指标，客观评估一只股票的供需状况。

用法:
    python core/supply_demand.py 603893
    python core/supply_demand.py 600519

输出:
    - 需求得分 (demand_score, 0-50)
    - 供给得分 (supply_score, 0-50)
    - 供需⭐ (1-5)
    - 6项子指标明细
"""

import logging
import sys
from typing import Optional
from datetime import datetime

import numpy as np
import pandas as pd

log = logging.getLogger("supply_demand")


def fetch_supply_demand_data(code: str) -> dict:
    """
    获取供需评估所需的多季度财务数据。

    Returns:
        {
            "code": str,
            "name": str,
            "quarters": list[dict],  # 最近N个季度的数据
            "indicators": dict,       # 当前各项指标
            "error": str | None,
        }
    """
    result = {
        "code": code,
        "name": "",
        "quarters": [],
        "indicators": {},
        "error": None,
    }

    try:
        import akshare as ak
        df = ak.stock_financial_abstract_new_ths(symbol=code, indicator="按报告期")
    except Exception as e:
        result["error"] = f"akshare获取失败: {e}"
        return result

    if df.empty:
        result["error"] = "无财务数据"
        return result

    try:
        # 按报告期分组，提取关键指标
        quarters_data = []
        report_dates = sorted(df["report_date"].unique(), reverse=True)

        for rd in report_dates[:8]:  # 取最近8个季度
            qd = df[df["report_date"] == rd].set_index("metric_name")
            row = {
                "report_date": str(rd),
                "report_name": qd.loc["report_name", "value"] if "report_name" in qd.index else "",
            }

            # 提取核心指标
            def _v(metric: str, default=None):
                if metric in qd.index:
                    val = qd.loc[metric, "value"]
                    if val is not None and not (isinstance(val, float) and pd.isna(val)):
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return default
                return default

            row["revenue"] = _v("operating_income_total")            # 营业总收入
            row["net_profit"] = _v("parent_holder_net_profit")       # 归母净利润
            row["gross_margin"] = _v("sale_gross_margin")            # 毛利率
            row["roe"] = _v("index_weighted_avg_roe")                # ROE
            row["inventory_turnover"] = _v("inventory_turnover_ratio")  # 存货周转率
            row["inventory_days"] = _v("inventory_turnover_days")    # 存货周转天数
            row["cf_ratio"] = _v("index_per_operating_cash_flow_net")  # 每股经营现金流
            row["eps"] = _v("basic_eps")                             # 基本每股收益
            row["revenue_yoy"] = _v("calculate_operating_income_total_yoy_growth_ratio")
            row["profit_yoy"] = _v("calculate_parent_holder_net_profit_yoy_growth_ratio")

            quarters_data.append(row)

        result["quarters"] = quarters_data

        # 当前指标
        if quarters_data:
            latest = quarters_data[0]
            result["indicators"] = {
                "revenue": latest.get("revenue"),
                "net_profit": latest.get("net_profit"),
                "gross_margin": latest.get("gross_margin"),
                "roe": latest.get("roe"),
                "inventory_turnover": latest.get("inventory_turnover"),
                "inventory_days": latest.get("inventory_days"),
                "revenue_yoy": latest.get("revenue_yoy"),
                "profit_yoy": latest.get("profit_yoy"),
            }

    except Exception as e:
        result["error"] = f"解析失败: {e}"

    return result


def calc_supply_demand(data: dict) -> dict:
    """
    计算供需评分（6项硬指标）。

    Returns:
        {
            "demand_score": float,   # 0-50 需求得分
            "supply_score": float,   # 0-50 供给得分
            "total_score": float,    # 0-100 总供需分
            "star": int,            # 1-5 供需⭐
            "details": dict,        # 6项子指标明细
            "summary": str,
        }
    """
    quarters = data.get("quarters", [])
    if not quarters or data.get("error"):
        return {
            "demand_score": 0, "supply_score": 0, "total_score": 0,
            "star": 0, "details": {}, "summary": "数据不可用",
        }

    q = quarters  # 从最新到最旧
    details = {}
    demand_total = 0
    supply_total = 0

    # ===================================================================
    # 需求端指标 (0-50)
    # ===================================================================

    # --- D1. 营收加速度 (0-15) ---
    # 比较最近2个季度营收YoY增速 vs 再之前2个季度
    rev_yoy_vals = [r.get("revenue_yoy") for r in q[:4] if r.get("revenue_yoy") is not None]
    if len(rev_yoy_vals) >= 2:
        recent_avg = np.mean(rev_yoy_vals[:min(2, len(rev_yoy_vals))])
        older_avg = np.mean(rev_yoy_vals[min(2, len(rev_yoy_vals)):min(4, len(rev_yoy_vals))]) if len(rev_yoy_vals) > 2 else recent_avg
        accel = recent_avg - older_avg
        if rev_yoy_vals[0] > 30 and accel > 5:
            d1_score = 15; d1_label = "高增长加速"
        elif rev_yoy_vals[0] > 20 and accel > 0:
            d1_score = 12; d1_label = "中高增长+稳"
        elif rev_yoy_vals[0] > 10:
            d1_score = 8; d1_label = "中速增长"
        elif rev_yoy_vals[0] > 0:
            d1_score = 5; d1_label = "低速增长"
        else:
            d1_score = 1; d1_label = "营收萎缩"
        d1_detail = f"最近YoY={rev_yoy_vals[0]:.1f}%, 变化={accel:+.1f}pp"
    elif len(rev_yoy_vals) == 1:
        if rev_yoy_vals[0] > 30:
            d1_score = 12; d1_label = "高增长"
            d1_detail = f"YoY={rev_yoy_vals[0]:.1f}%"
        elif rev_yoy_vals[0] > 10:
            d1_score = 8; d1_label = "中速增长"
            d1_detail = f"YoY={rev_yoy_vals[0]:.1f}%"
        elif rev_yoy_vals[0] > 0:
            d1_score = 5; d1_label = "低速增长"
            d1_detail = f"YoY={rev_yoy_vals[0]:.1f}%"
        else:
            d1_score = 1; d1_label = "营收萎缩"
            d1_detail = f"YoY={rev_yoy_vals[0]:.1f}%"
    else:
        d1_score = 5; d1_label = "数据不足"; d1_detail = "无YoY数据"

    details["D1.营收加速度"] = {"score": d1_score, "max": 15, "label": d1_label, "detail": d1_detail}

    # --- D2. 利润质量 (0-15) ---
    # 净利增速 vs 营收增速：利润增速远超营收说明规模效应
    profit_yoy = q[0].get("profit_yoy")
    revenue_yoy = q[0].get("revenue_yoy")
    if profit_yoy is not None and revenue_yoy is not None and revenue_yoy > 0:
        quality_ratio = profit_yoy / revenue_yoy
        if quality_ratio >= 1.5:
            d2_score = 15; d2_label = "规模效应显著(利润>>营收)"
        elif quality_ratio >= 1.0:
            d2_score = 12; d2_label = "利润优于营收"
        elif quality_ratio >= 0.5:
            d2_score = 8; d2_label = "利润匹配营收"
        else:
            d2_score = 4; d2_label = "利润落后营收(增收不增利)"
        d2_detail = f"净利增速{profit_yoy:.1f}% / 营收增速{revenue_yoy:.1f}% = {quality_ratio:.2f}"
    elif profit_yoy is not None and profit_yoy > 0:
        d2_score = 10; d2_label = "利润正增长"
        d2_detail = f"净利增速={profit_yoy:.1f}%"
    elif profit_yoy is not None:
        d2_score = 3; d2_label = "利润下滑"
        d2_detail = f"净利增速={profit_yoy:.1f}%"
    else:
        d2_score = 5; d2_label = "数据不足"; d2_detail = ""

    details["D2.利润质量"] = {"score": d2_score, "max": 15, "label": d2_label, "detail": d2_detail}

    # --- D3. PEG估值匹配 (0-20) ---
    # PEG = PE / 净利增速，<1合理，>2严重高估
    peg = None
    if profit_yoy is not None and profit_yoy > 0:
        # PE from the quote data (passed in later), use default calculation
        # We compute PEG here; PE will be merged from quant_score
        profit_yoy_val = profit_yoy
        if profit_yoy_val > 0:
            # Placeholder - PE will be filled by caller
            details["D3.PEG"] = {"score": 0, "max": 20, "label": "待PE数据", "detail": f"净利增速={profit_yoy_val:.1f}%"}
    else:
        details["D3.PEG"] = {"score": 3, "max": 20, "label": "利润负增长/PEG无效", "detail": ""}

    demand_total = d1_score + d2_score

    # ===================================================================
    # 供给端指标 (0-50)
    # ===================================================================

    # --- S1. 毛利率趋势 (0-15) ---
    gm_vals = [r.get("gross_margin") for r in q[:4] if r.get("gross_margin") is not None]
    if len(gm_vals) >= 2:
        gm_trend = gm_vals[0] - gm_vals[-1]
        if gm_vals[0] > 40 and gm_trend > 2:
            s1_score = 15; s1_label = "高毛利+持续提升(定价权强)"
        elif gm_vals[0] > 30 and gm_trend > 0:
            s1_score = 12; s1_label = "毛利上升(供给偏紧)"
        elif gm_vals[0] > 30:
            s1_score = 9; s1_label = "毛利稳定(供给平衡)"
        elif gm_vals[0] > 20:
            s1_score = 6; s1_label = "毛利偏低"
        else:
            s1_score = 3; s1_label = "毛利低(竞争激烈/无定价权)"
        s1_detail = f"最新={gm_vals[0]:.1f}%, 趋势={gm_trend:+.1f}pp (近{gm_vals.index(gm_vals[-1])+1}季)"
    elif len(gm_vals) == 1:
        if gm_vals[0] > 40:
            s1_score = 12; s1_label = "高毛利"
            s1_detail = f"毛利率={gm_vals[0]:.1f}%"
        elif gm_vals[0] > 30:
            s1_score = 9; s1_label = "毛利良好"
            s1_detail = f"毛利率={gm_vals[0]:.1f}%"
        else:
            s1_score = 5; s1_label = "毛利偏低"
            s1_detail = f"毛利率={gm_vals[0]:.1f}%"
    else:
        s1_score = 5; s1_label = "数据不足"; s1_detail = ""

    details["S1.毛利率趋势"] = {"score": s1_score, "max": 15, "label": s1_label, "detail": s1_detail}

    # --- S2. 存货周转 (0-15) ---
    inv_vals = [r.get("inventory_turnover") for r in q[:4] if r.get("inventory_turnover") is not None]
    if len(inv_vals) >= 2:
        inv_trend = inv_vals[0] - inv_vals[-1]
        if inv_trend > 1:
            s2_score = 15; s2_label = "周转加速(供不应求)"
        elif inv_trend > 0:
            s2_score = 12; s2_label = "周转改善"
        elif inv_trend > -1:
            s2_score = 8; s2_label = "周转稳定"
        else:
            s2_score = 4; s2_label = "周转减速(库存积压)"
        s2_detail = f"周转率={inv_vals[0]:.2f}, 趋势={inv_trend:+.2f}"
    elif len(inv_vals) == 1:
        if inv_vals[0] > 5:
            s2_score = 12; s2_label = "快周转"
            s2_detail = f"周转率={inv_vals[0]:.2f}"
        elif inv_vals[0] > 2:
            s2_score = 8; s2_label = "正常周转"
            s2_detail = f"周转率={inv_vals[0]:.2f}"
        else:
            s2_score = 4; s2_label = "慢周转"
            s2_detail = f"周转率={inv_vals[0]:.2f}"
    else:
        s2_score = 6; s2_label = "数据不足"; s2_detail = ""

    details["S2.存货周转"] = {"score": s2_score, "max": 15, "label": s2_label, "detail": s2_detail}

    # --- S3. ROE水平 (0-20) ---
    roe_vals = [r.get("roe") for r in q[:4] if r.get("roe") is not None]
    if roe_vals:
        latest_roe = roe_vals[0]
        if latest_roe and latest_roe > 20:
            s3_score = 20; s3_label = "极高ROE(>20%)"
        elif latest_roe and latest_roe > 12:
            s3_score = 16; s3_label = "高ROE(12-20%)"
        elif latest_roe and latest_roe > 8:
            s3_score = 12; s3_label = "中高ROE(8-12%)"
        elif latest_roe and latest_roe > 5:
            s3_score = 8; s3_label = "中低ROE(5-8%)"
        else:
            s3_score = 4; s3_label = "低ROE(<5%)"
        s3_detail = f"ROE={latest_roe:.1f}%"
    else:
        s3_score = 8; s3_label = "数据不足"; s3_detail = ""

    details["S3.ROE水平"] = {"score": s3_score, "max": 20, "label": s3_label, "detail": s3_detail}

    supply_total = s1_score + s2_score + s3_score

    # ===================================================================
    # PEG 修正（需要外部注入PE值，先留占位）
    # ===================================================================
    if "D3.PEG" in details and details["D3.PEG"]["label"] == "待PE数据":
        details["D3.PEG"]["max"] = 20
        details["D3.PEG"]["score"] = 8  # 默认中值

    # ===================================================================
    # 综合
    # ===================================================================
    total_score = demand_total + supply_total + details.get("D3.PEG", {}).get("score", 0)
    total_score = max(0, min(100, total_score))

    if total_score >= 80:
        star = 5
        summary = "供需双旺，产品供不应求，有定价权"
    elif total_score >= 65:
        star = 4
        summary = "需求强劲，供给偏紧，基本面扎实"
    elif total_score >= 50:
        star = 3
        summary = "供需平衡，增速尚可，有待催化剂"
    elif total_score >= 35:
        star = 2
        summary = "需求疲软或供给过剩，谨慎"
    else:
        star = 1
        summary = "供需双弱，基本面堪忧"

    return {
        "demand_score": demand_total,
        "supply_score": supply_total,
        "total_score": total_score,
        "star": star,
        "details": details,
        "summary": summary,
    }


def merge_pe_into_peg(heat: dict, pe_ttm: float) -> dict:
    """将 PE 值注入到供需评估的 PEG 计算中。"""
    details = heat.get("details", {})
    if "D3.PEG" in details:
        profit_yoy = None
        for d in details.values():
            pass  # extract from somewhere...
        # PEG = PE / profit_yoy
        # We need profit_yoy here - let's store it in the heat dict
        if "profit_yoy" in heat:
            py = heat["profit_yoy"]
            if py and py > 0 and pe_ttm > 0:
                peg = pe_ttm / py
                if peg < 1:
                    peg_score = 20
                    peg_label = f"PEG={peg:.2f}(<1, 低估)"
                elif peg < 1.5:
                    peg_score = 15
                    peg_label = f"PEG={peg:.2f}(1-1.5, 合理)"
                elif peg < 2.5:
                    peg_score = 10
                    peg_label = f"PEG={peg:.2f}(1.5-2.5, 偏贵)"
                else:
                    peg_score = 4
                    peg_label = f"PEG={peg:.2f}(>2.5, 严重高估)"
                old_score = details["D3.PEG"].get("score", 0)
                details["D3.PEG"] = {"score": peg_score, "max": 20, "label": peg_label, "detail": f"PE={pe_ttm:.1f}/{py:.1f}%"}
                # Recalculate total
                new_total = heat.get("demand_score", 0) + heat.get("supply_score", 0) + peg_score
                new_total = max(0, min(100, new_total))
                heat["total_score"] = new_total
                # Recalculate star
                if new_total >= 80: heat["star"] = 5
                elif new_total >= 65: heat["star"] = 4
                elif new_total >= 50: heat["star"] = 3
                elif new_total >= 35: heat["star"] = 2
                else: heat["star"] = 1
    return heat


def format_supply_demand_output(result: dict) -> str:
    """格式化供需评估输出。"""
    lines = []
    star = result.get("star", 0)
    star_str = "*" * star + "-" * (5 - star)
    total = result.get("total_score", 0)

    lines.append(f"## 供需评估 — {result.get('name', result.get('code', '?'))}")
    lines.append(f"> 供需评级: {star}/5 | 总供需分: {total:.0f}/100")
    lines.append(f"> 需求端: {result.get('demand_score', 0):.0f}/50 | 供给端: {result.get('supply_score', 0):.0f}/50")
    lines.append(f"> {result.get('summary', '')}")
    lines.append("")

    details = result.get("details", {})
    if details:
        lines.append("### 供需六维指标明细")
        lines.append(f"{'指标':<18} {'得分':<5} {'判定':<22} {'数值/依据'}")
        lines.append("-" * 68)
        for key, d in details.items():
            lines.append(f"{key:<18} {d['score']:>2}/{d['max']:<2} {d['label']:<22} {d.get('detail', '')}")
        lines.append("")

    quarters = result.get("quarters", [])
    if len(quarters) >= 3:
        lines.append("### 季度营收趋势")
        lines.append(f"{'季度':<14} {'营收(亿)':<12} {'净利(亿)':<12} {'毛利率':<8} {'营收YoY'}")
        lines.append("-" * 60)
        for r in quarters[:4]:
            rev = r.get("revenue")
            np_ = r.get("net_profit")
            gm = r.get("gross_margin")
            yoy = r.get("revenue_yoy")
            rev_str = f"{rev/1e8:.2f}" if rev else "?"
            np_str = f"{np_/1e8:.2f}" if np_ else "?"
            gm_str = f"{gm:.1f}%" if gm else "?"
            yoy_str = f"{yoy:+.1f}%" if yoy is not None else "?"
            lines.append(f"{r['report_date']:<14} {rev_str:<12} {np_str:<12} {gm_str:<8} {yoy_str}")

    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="供需评估")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("--pe", type=float, default=None, help="PE(TTM)值，用于PEG计算")

    args = parser.parse_args()

    data = fetch_supply_demand_data(args.code)
    if data.get("error"):
        print(f"错误: {data['error']}")
        sys.exit(1)

    result = calc_supply_demand(data)

    # 注入PE
    if args.pe:
        profit_yoy = None
        if data.get("quarters"):
            profit_yoy = data["quarters"][0].get("profit_yoy")
        if profit_yoy:
            result["profit_yoy"] = profit_yoy
            result = merge_pe_into_peg(result, args.pe)
    elif data.get("quarters") and data["quarters"][0].get("profit_yoy"):
        result["profit_yoy"] = data["quarters"][0]["profit_yoy"]

    result["name"] = data.get("name", "")
    result["quarters"] = data.get("quarters", [])
    print(format_supply_demand_output(result))
