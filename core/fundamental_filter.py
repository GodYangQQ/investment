#!/usr/bin/env python3
"""
基本面过滤模块
对量化排名结果进行基本面硬过滤，剔除：
  - PE < 0（亏损）
  - PE > 200（估值离谱）
  - 以及可选：通过 akshare 查 ROE/毛利率/现金流

用法：
    from fundamental_filter import quick_pe_filter, batch_fetch_fundamentals, apply_fundamental_filter

    # 快速PE过滤（无需akshare，基于已有行情数据）
    df = quick_pe_filter(df, max_pe=200, exclude_negative_pe=True)

    # 深度过滤（需要akshare，仅对Top N执行）
    result = apply_fundamental_filter(top_df, min_roe=8, min_gross_margin=15)
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# akshare 是否为可用状态
try:
    import akshare as ak
    _HAS_AKSHARE = True
except ImportError:
    _HAS_AKSHARE = False


# ============================================================================
# 1. 快速 PE 过滤（无需 akshare）
# ============================================================================

def quick_pe_filter(
    df: pd.DataFrame,
    max_pe: float = 200,
    exclude_negative_pe: bool = True,
    pe_col: str = "PE_TTM",
) -> pd.DataFrame:
    """
    基于 PE_TTM 快速过滤。不需要额外网络请求。

    Args:
        df: 包含 PE_TTM 列的 DataFrame
        max_pe: PE 上限，超过此值视为估值离谱
        exclude_negative_pe: 是否排除亏损（PE < 0）

    Returns:
        过滤后的 DataFrame，新增 "PE过滤" 列标记被过滤的原因
    """
    reasons = []

    for _, row in df.iterrows():
        pe = row.get(pe_col, 0)
        reasons_list = []

        if exclude_negative_pe and (pe < 0 or pd.isna(pe)):
            reasons_list.append("亏损" if pe < 0 else "PE缺失")
        elif pe == 0:
            reasons_list.append("PE为0(数据异常)")
        elif pe > max_pe:
            reasons_list.append(f"PE({pe:.0f})>{max_pe}")

        reasons.append(",".join(reasons_list) if reasons_list else "通过")

    df = df.copy()
    df["PE过滤"] = reasons
    before = len(df)
    df = df[df["PE过滤"] == "通过"].copy()
    log.info("PE过滤: %d → %d (剔除%d只)", before, len(df), before - len(df))
    return df


# ============================================================================
# 2. 深度基本面获取（需 akshare，对少量股票）
# ============================================================================

def fetch_single_fundamentals(code: str) -> dict:
    """
    获取单只股票的关键基本面指标（通过 akshare）。
    返回: {"roe": float, "gross_margin": float, "net_margin": float,
           "net_profit_yoy": float, "revenue_yoy": float,
           "cf_ratio": float, "debt_ratio": float, "error": str|None}
    """
    result = {
        "roe": None, "gross_margin": None, "net_margin": None,
        "net_profit_yoy": None, "revenue_yoy": None,
        "cf_ratio": None, "debt_ratio": None, "error": None,
    }

    if not _HAS_AKSHARE:
        result["error"] = "akshare未安装"
        return result

    try:
        # 使用同花顺新版财务摘要接口（stock_financial_abstract 旧版已不可用）
        df = ak.stock_financial_abstract_new_ths(symbol=code, indicator="按报告期")
    except Exception as e:
        result["error"] = f"获取失败: {e}"
        return result

    try:
        if df.empty:
            result["error"] = "无数据"
            return result

        # 取最新报告期数据
        latest_date = df["report_date"].max()
        latest = df[df["report_date"] == latest_date].set_index("metric_name")

        def _get(metric: str) -> Optional[float]:
            """从 latest 中按 metric_name 提取数值"""
            if metric in latest.index:
                val = latest.loc[metric, "value"]
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    return None
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None
            return None

        result["roe"] = _get("index_weighted_avg_roe")
        result["gross_margin"] = _get("sale_gross_margin")
        result["net_margin"] = _get("sale_net_interest_ratio")
        result["debt_ratio"] = _get("assets_debt_ratio")
        result["net_profit_yoy"] = _get("calculate_parent_holder_net_profit_yoy_growth_ratio")
        result["revenue_yoy"] = _get("calculate_operating_income_total_yoy_growth_ratio")

        # 经营现金流/净利润比
        # 计算公式: (每股经营现金流 × 总股本) / 归母净利润
        # 总股本 = 归母净利润 / 基本每股收益
        net_profit = _get("parent_holder_net_profit")
        eps_cf = _get("index_per_operating_cash_flow_net")
        eps = _get("basic_eps")
        if net_profit and net_profit > 0 and eps_cf is not None and eps and eps > 0:
            total_shares = net_profit / eps
            cf_total = eps_cf * total_shares
            result["cf_ratio"] = cf_total / net_profit
        elif net_profit and net_profit > 0 and eps_cf is not None and eps_cf > 0:
            result["cf_ratio"] = 1.0  # 正现金流但无法精确计算，标记为正
        elif net_profit and net_profit > 0 and eps_cf is not None:
            result["cf_ratio"] = -1.0  # 负现金流

    except Exception as e:
        result["error"] = f"解析失败: {e}"

    return result


def batch_fetch_fundamentals(codes: list[str]) -> pd.DataFrame:
    """
    批量获取基本面数据（串行，每次间隔0.3秒避免被封）。

    Args:
        codes: 股票代码列表

    Returns:
        DataFrame，列为 code, roe, gross_margin, net_margin, cf_ratio, debt_ratio, error
    """
    import time

    results = []
    total = len(codes)

    for i, code in enumerate(codes):
        if (i + 1) % 20 == 0:
            log.info("基本面获取进度: %d/%d", i + 1, total)

        fin = fetch_single_fundamentals(code)
        fin["code"] = code
        results.append(fin)
        time.sleep(0.3)  # akshare 频率限制

    return pd.DataFrame(results)


# ============================================================================
# 3. 基本面硬过滤规则
# ============================================================================

# 默认过滤阈值
DEFAULT_FUNDAMENTAL_RULES = {
    "min_roe": 5.0,            # ROE 警告阈值（低于此值标记但不排除）
    "min_roe_hard": 2.0,       # ROE 硬底线（低于此值才硬排除）
    "min_cf_ratio": 0.0,       # 现金流/净利润底线（<0标记但不硬排除）
    "min_cf_ratio_hard": -0.5, # 现金流硬底线
    "max_debt_ratio": 85.0,    # 资产负债率 < 85%
    "exclude_negative_pe": False,
    "max_pe": 200,
}

# ============================================================================
# 3a. 亏损股分级处理（替代一刀切）
# ============================================================================

def classify_loss_stock(
    pe: float,
    gross_margin: Optional[float] = None,
    revenue_yoy: Optional[float] = None,
    net_margin: Optional[float] = None,
) -> tuple[str, str, float]:
    """
    对亏损股（PE < 0）进行三级分类，替代一刀切排除。

    Level 1 — ❌ 真垃圾：亏损 + 营收严重下滑(<-10%)
        硬排除，不分析
    
    Level 2 — ⚠️ 亏损潜力股：亏损 但 毛利率>30% + 营收高增长(>20%)
        不排除，但仓位×0.3，仅趋势轨可行（不设止盈，让故事兑现）
    
    Level 3 — 🟡 亏损待观察：其他亏损情况
        不排除，仓位×0.5，需人工判断

    Returns:
        (level_label, reason, position_multiplier)
    """
    # 如果盈利，不属于亏损股
    if pe > 0:
        return "盈利", "盈利", 1.0

    # PE=0 可能是数据缺失，保守处理
    if pe == 0:
        return "unknown", "PE=0(数据缺失)", 0.5

    # --- 亏损股分级 ---

    # Level 1: 真垃圾（营收严重下滑）
    if revenue_yoy is not None and revenue_yoy < -10:
        return "❌ 真垃圾", f"亏损+营收增速{revenue_yoy:.1f}%", 0.0  # 硬排除

    # Level 2: 潜力股（高毛利+高增长）
    if (gross_margin is not None and gross_margin > 30) and \
       (revenue_yoy is not None and revenue_yoy > 20):
        return "⚠️ 亏损潜力股", f"毛利率{gross_margin:.1f}%+营收增速{revenue_yoy:.1f}%", 0.3

    # Level 3: 待观察
    return "🟡 亏损待观察", f"PE<0,毛利率={gross_margin},营收增速={revenue_yoy}", 0.5


def check_single_stock(
    pe: float,
    roe: Optional[float] = None,
    gross_margin: Optional[float] = None,
    cf_ratio: Optional[float] = None,
    debt_ratio: Optional[float] = None,
    revenue_yoy: Optional[float] = None,
    net_margin: Optional[float] = None,
    rules: dict = None,
) -> tuple[bool, list[str], float]:
    """
    检查单只股票是否符合基本面要求。

    Returns:
        (是否通过, 未通过原因列表, 仓位乘数)
        仓位乘数: 1.0=正常, 0.x=降权, 0.0=硬排除
    """
    if rules is None:
        rules = DEFAULT_FUNDAMENTAL_RULES

    failures = []
    position_mult = 1.0

    # === PE 检查（含亏损股分级） ===
    if pe < 0:
        # ⚠️ 不再一刀切，改用分级
        level, reason, mult = classify_loss_stock(
            pe, gross_margin, revenue_yoy, net_margin
        )
        position_mult *= mult
        if mult == 0.0:
            failures.append(f"亏损股({level}): {reason}")
        else:
            # 不加入 failures（不硬排除），但降权
            pass
    elif pe == 0:
        # PE=0 可能是数据缺失
        position_mult *= 0.7  # 降权但不排除
    elif pe > rules.get("max_pe", 200):
        failures.append(f"PE过高({pe:.0f})")

    # === ROE 检查（仅对盈利股，分两档） ===
    if pe > 0 and roe is not None:
        # 硬排除: ROE < min_roe_hard (默认2%)
        hard_limit = rules.get("min_roe_hard", 2.0)
        if roe < hard_limit:
            failures.append(f"ROE极低({roe:.1f}%)")
        # 警告但不排除: ROE < min_roe (默认5%)
        elif roe < rules.get("min_roe", 5.0):
            # 不加入 failures（不硬排除），降权
            position_mult *= 0.6

    # === 现金流检查（仅对盈利股） ===
    if pe > 0 and cf_ratio is not None:
        hard_cf = rules.get("min_cf_ratio_hard", -0.5)
        if cf_ratio < hard_cf:
            failures.append(f"现金流极差(CF/NI={cf_ratio:.2f})")
        elif cf_ratio < rules.get("min_cf_ratio", 0.0):
            # 现金流为负但不太严重 → 降权不排除
            position_mult *= 0.7

    # === 负债率检查 ===
    if debt_ratio is not None and rules.get("max_debt_ratio"):
        if debt_ratio > rules["max_debt_ratio"]:
            failures.append(f"负债过高({debt_ratio:.1f}%)")

    return (len(failures) == 0, failures, position_mult)


def apply_fundamental_filter(
    df: pd.DataFrame,
    deep_check_top: int = 200,
    rules: dict = None,
) -> pd.DataFrame:
    """
    对量化排名结果应用基本面过滤。

    流程：
    1. 先做快速 PE 过滤（全量，但不再一刀切排除亏损）
    2. 对前 deep_check_top 只做深度基本面检查（需 akshare）
    3. 应用亏损股分级 + 各项过滤
    4. 按可买度排序输出最终结果

    Args:
        df: 包含 "PE_TTM", "代码" 列的 DataFrame
        deep_check_top: 对前 N 只做深度检查
        rules: 过滤规则字典

    Returns:
        过滤后的 DataFrame，新增 ROE, 毛利率, 现金流比, 负债率, 
        基本面通过, 基本面原因, 仓位乘数, 亏损等级 列
    """
    if rules is None:
        rules = DEFAULT_FUNDAMENTAL_RULES

    # Step 1: PE 快速过滤（只过滤 PE>200 离谱的，不再排亏损）
    df = quick_pe_filter(df, max_pe=rules.get("max_pe", 200),
                         exclude_negative_pe=rules.get("exclude_negative_pe", False))

    if len(df) == 0:
        log.warning("PE过滤后无剩余股票")
        return df

    # Step 2: 深度基本面检查（仅对 Top N）
    deep_n = min(deep_check_top, len(df))
    top_codes = df.head(deep_n)["代码"].tolist()

    log.info("对 Top %d 进行深度基本面检查...", deep_n)
    fin_df = batch_fetch_fundamentals(top_codes)

    # Step 3: 合并基本面数据
    fin_df = fin_df.rename(columns={
        "code": "代码",
        "roe": "ROE",
        "gross_margin": "毛利率",
        "net_margin": "净利率",
        "cf_ratio": "现金流比",
        "debt_ratio": "负债率",
        "revenue_yoy": "营收增速",
    })

    merge_cols = ["代码", "ROE", "毛利率", "净利率", "现金流比", "负债率", "营收增速"]
    avail_cols = [c for c in merge_cols if c in fin_df.columns]
    df = df.merge(fin_df[avail_cols], on="代码", how="left")

    # Step 4: 逐只检查（含亏损股分级）
    filter_results = []
    for _, row in df.iterrows():
        passed, failures, pos_mult = check_single_stock(
            pe=row.get("PE_TTM", 0),
            roe=row.get("ROE") if "ROE" in df.columns else None,
            gross_margin=row.get("毛利率") if "毛利率" in df.columns else None,
            cf_ratio=row.get("现金流比") if "现金流比" in df.columns else None,
            debt_ratio=row.get("负债率") if "负债率" in df.columns else None,
            revenue_yoy=row.get("营收增速") if "营收增速" in df.columns else None,
            net_margin=row.get("净利率") if "净利率" in df.columns else None,
            rules=rules,
        )
        # 确定过滤原因
        pe_val = row.get("PE_TTM", 0)
        if pe_val < 0:
            level, loss_reason, _ = classify_loss_stock(
                pe_val,
                row.get("毛利率") if "毛利率" in df.columns else None,
                row.get("营收增速") if "营收增速" in df.columns else None,
                row.get("净利率") if "净利率" in df.columns else None,
            )
            if pos_mult == 0:
                reason = f"❌ {loss_reason}"
            else:
                suffix = f" 仓位×{pos_mult:.1f}" if pos_mult < 1.0 else ""
                reason = f"{level}{suffix}"
        elif passed:
            # 通过但有降权？
            if pos_mult < 1.0:
                warnings = []
                roe_v = row.get("ROE")
                cf_v = row.get("现金流比")
                pe_v = row.get("PE_TTM", 0)
                if roe_v is not None and pe_v > 0 and roe_v < rules.get("min_roe", 5.0):
                    warnings.append(f"ROE{roe_v:.1f}%")
                if cf_v is not None and pe_v > 0 and cf_v < 0:
                    warnings.append(f"CF负{cf_v:.1f}")
                if warnings:
                    reason = f"⚠️ 降权({','.join(warnings)}) 仓位×{pos_mult:.1f}"
                else:
                    reason = "✅ 通过"
            else:
                reason = "✅ 通过"
        else:
            reason = "❌ " + "; ".join(failures)

        filter_results.append({
            "passed": pos_mult > 0,  # 仓位乘数>0即不排除
            "reason": reason,
            "position_mult": pos_mult,
        })

    df = df.copy()
    df["基本面通过"] = [r["passed"] for r in filter_results]
    df["基本面原因"] = [r["reason"] for r in filter_results]
    df["仓位乘数"] = [r["position_mult"] for r in filter_results]

    passed_count = df["基本面通过"].sum()
    log.info("深度检查结果: %d/%d 通过基本面过滤", passed_count, len(df))

    return df


# ============================================================================
# CLI 测试
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 测试 fetch_single_fundamentals
    print("=== 测试: 单只基本面获取 ===")
    for code in ["600519", "600026", "002475"]:
        fin = fetch_single_fundamentals(code)
        print(f"  {code}: ROE={fin['roe']}, 毛利率={fin['gross_margin']}, "
              f"CF/NI={fin['cf_ratio']}, 负债={fin['debt_ratio']}, err={fin['error']}")

    # 测试 PE 过滤
    print("\n=== 测试: PE 过滤 ===")
    test_df = pd.DataFrame([
        {"代码": "600519", "PE_TTM": 25},
        {"代码": "000001", "PE_TTM": -5},
        {"代码": "002475", "PE_TTM": 300},
        {"代码": "600026", "PE_TTM": 22},
    ])
    filtered = quick_pe_filter(test_df)
    print(filtered[["代码", "PE_TTM", "PE过滤"]].to_string(index=False))
