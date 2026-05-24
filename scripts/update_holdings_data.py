#!/usr/bin/env python3
"""
持仓数据生成器
1. 读取 data/my_pool.csv 中有持仓的股票（成本价+持仓量均不为空）
2. 逐个运行 quant_score.py 获取量化评分+动量+MA/布林/ATR
3. 输出 dashboards/holdings_data.json 供 positions.html 读取
用法: python scripts/update_holdings_data.py
"""

import csv
import json
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
POOL_PATH = ROOT / "data" / "my_pool.csv"
OUTPUT_PATH = ROOT / "dashboards" / "holdings_data.json"

# ─── 手动填充：基本面⭐/概念⭐/等级（基于 AI 分析结果，不随量化自动变化）───
MANUAL_GRADES = {
    "002463": {"name": "沪电股份",   "fStar": 4, "cStar": 5, "grade": "A+", "drawdownLine": 10},
    "600026": {"name": "中远海能",  "fStar": 4, "cStar": 3, "grade": "A",  "drawdownLine": 8},
    "603893": {"name": "瑞芯微",    "fStar": 4, "cStar": 5, "grade": "A+", "drawdownLine": 10},
    "002466": {"name": "天齐锂业",  "fStar": 2, "cStar": 3, "grade": "B",  "drawdownLine": 6},
    "002475": {"name": "立讯精密",  "fStar": 2, "cStar": 3, "grade": "B",  "drawdownLine": 6},
    "601138": {"name": "工业富联",  "fStar": 2, "cStar": 3, "grade": "B",  "drawdownLine": 6},
}


def read_positions():
    """Read my_pool.csv, return list of dicts for stocks with positions."""
    positions = []
    with open(POOL_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["代码"].strip()
            cost = row["成本价"].strip()
            shares = row["持仓量"].strip()
            if not code or not cost or not shares:
                continue
            try:
                float(cost)
                float(shares)
            except ValueError:
                continue
            date = row["买入日期"].strip() or ""
            tier = row["层级"].strip() or "核心"
            name = row["名称"].strip() or code
            positions.append({
                "code": code, "name": name, "tier": tier,
                "cost": float(cost), "shares": int(float(shares)),
                "buyDate": date,
            })
    return positions


def run_quant_score(code):
    """Run quant_score.py for a single code, return parsed JSON."""
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "core" / "quant_score.py"), code,
             "--output", str(Path(os.environ.get("TEMP", "/tmp")) / f"qs_{code}.json")],
            capture_output=True, text=True, timeout=30,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        tmp_path = Path(os.environ.get("TEMP", "/tmp")) / f"qs_{code}.json"
        if tmp_path.exists():
            with open(tmp_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"  ⚠ {code} 量化评分失败: {e}")
    return None


def main():
    positions = read_positions()
    if not positions:
        print("❌ my_pool.csv 中无持仓数据")
        sys.exit(1)

    print(f"📊 共 {len(positions)} 只持仓，开始获取量化数据...")
    result = []

    for i, pos in enumerate(positions):
        code = pos["code"]
        name = pos["name"]
        print(f"  [{i+1}/{len(positions)}] {code} {name} ...", end=" ", flush=True)
        qs = run_quant_score(code)

        # Extract signals from quant_score output
        signals = {}
        momentum = {}
        fundamental = {}
        score_total = 0
        buyability = "❓"
        momentum_total = 0
        momentum_grade = ""
        rsi_detail = {"rsi_current": 0, "rsi_slope_5d": 0, "rsi_arrow": "—"}
        roe = 0
        gross_margin = 0
        debt_ratio = 0
        cf_ratio = 0

        if qs:
            sig = qs.get("signals") or {}
            signals = {
                "ma5":         sig.get("ma5", 0),
                "ma10":        sig.get("ma10", 0),
                "ma20":        sig.get("ma20", 0),
                "ma60":        sig.get("ma60", 0),
                "bb_upper":    sig.get("bb_upper", 0),
                "bb_lower":    sig.get("bb_lower", 0),
                "bb_mid":      sig.get("bb_mid", 0),
                "atr14":       sig.get("atr14", 0),
                "rsi14":       sig.get("rsi14", 0),
                "high_20d":    sig.get("high_20d", 0),
                "low_20d":     sig.get("low_20d", 0),
                "high_60d":    sig.get("high_60d", 0),
                "low_60d":     sig.get("low_60d", 0),
            }
            sc = qs.get("score") or {}
            score_total = sc.get("total", 0)
            buyability = sc.get("buyability", "❓")

            mom = qs.get("momentum") or {}
            momentum_total = mom.get("momentum_total", 0) if mom.get("data_available") else 3
            momentum_grade = mom.get("momentum_grade", "")

            rsi_d = qs.get("rsi_detail") or {}
            rsi_detail = {
                "rsi_current": rsi_d.get("rsi_current", sig.get("rsi14", 0)),
                "rsi_slope_5d": rsi_d.get("rsi_slope_5d", 0),
                "rsi_arrow": rsi_d.get("rsi_arrow", "—"),
            }

            fa = qs.get("fundamental_auto") or {}
            roe = fa.get("roe", 0)
            gross_margin = fa.get("gross_margin", 0)
            debt_ratio = fa.get("debt_ratio", 0)
            cf_ratio = fa.get("cf_ratio", 0)

        else:
            print("⚠ 使用默认值")
            score_total = 50
            buyability = "⚠ 数据异常"

        # Merge manual grades
        grade_info = MANUAL_GRADES.get(code, {"fStar": 2, "cStar": 2, "grade": "B", "drawdownLine": 6})
        final_name = grade_info.pop("name", name)

        entry = {
            "code": code,
            "name": final_name,
            "tier": pos["tier"],
            "cost": pos["cost"],
            "shares": pos["shares"],
            "buyDate": pos["buyDate"],
            "grade": grade_info["grade"],
            "drawdownLine": grade_info["drawdownLine"],
            "fStar": grade_info["fStar"],
            "cStar": grade_info["cStar"],
            "scoreTotal": score_total,
            "buyability": buyability,
            "momentum_total": momentum_total,
            "momentum_grade": momentum_grade,
            "signals": signals,
            "rsi_detail": rsi_detail,
            "fundamental": {
                "roe": roe,
                "gross_margin": gross_margin,
                "debt_ratio": debt_ratio,
                "cf_ratio": cf_ratio,
            },
        }
        result.append(entry)
        print("✓")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已输出: {OUTPUT_PATH}")
    print(f"   HTML 刷新即可获取最新量化数据。")


if __name__ == "__main__":
    main()
