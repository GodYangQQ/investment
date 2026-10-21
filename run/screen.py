"""
Undervalued Stock Screener (SOXX)
Combines quantitative metrics (yfinance) with optional LLM analysis
to rank SOXX top-20 names by undervaluation.
"""

import os
import sys
import time
import json
import argparse
import logging
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

import pandas as pd
from openai import OpenAI

from market_filter import get_market, filter_by_market, ALL_MARKETS
from universe import get_soxx_universe
from us_data import fetch_batch_quotes, fetch_financial_data, fetch_daily_kline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def fetch_realtime_quotes() -> pd.DataFrame:
    universe = get_soxx_universe(top_n=20, live=True)
    codes = [s["code"] for s in universe]
    names = {s["code"]: s["name"] for s in universe}
    quotes = fetch_batch_quotes(codes)
    rows = []
    for s in universe:
        code = s["code"]
        q = quotes.get(code) or {}
        try:
            df = fetch_daily_kline(code, days=260)
            pct_60d = (df["close"].iloc[-1] / df["close"].iloc[-61] - 1) * 100 if len(df) >= 61 else 0
            pct_ytd = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100 if len(df) else 0
        except Exception:
            pct_60d = pct_ytd = 0
        rows.append({
            "code": code,
            "name": q.get("name") or names.get(code, code),
            "price": q.get("price", 0),
            "pct_change": q.get("pct_change", 0),
            "pe_ttm": q.get("pe_ttm", 0),
            "pb": q.get("pb", 0),
            "total_mv": (q.get("market_cap") or q.get("total_mv_yi", 0) * 1e8),
            "turnover": q.get("turnover", 0),
            "pct_60d": pct_60d,
            "pct_ytd": pct_ytd,
        })
    df = pd.DataFrame(rows)
    log.info("Fetched %d SOXX names", len(df))
    return df


def fetch_financial_indicators(code: str) -> Optional[pd.DataFrame]:
    fin = fetch_financial_data(code)
    if not fin:
        return None
    return pd.DataFrame([fin])


def fetch_individual_info(code: str) -> dict:
    return {"行业": get_market(code) or "半导体"}


# ---------------------------------------------------------------------------
# Quantitative scoring
# ---------------------------------------------------------------------------

def compute_quant_score(row: pd.Series) -> float:
    """
    Simple quantitative undervaluation score (0-100, higher = more undervalued).
    Factors: PE percentile, PB percentile, ROE proxy, momentum.
    """
    score = 0.0
    pe = row.get("pe_ttm", 0)
    pb = row.get("pb", 0)

    # Low PE bonus (0-30 points)
    if 0 < pe < 10:
        score += 30
    elif pe < 15:
        score += 20
    elif pe < 20:
        score += 10
    elif pe < 30:
        score += 5

    # Low PB bonus (0-30 points)
    if 0 < pb < 1:
        score += 30
    elif pb < 1.5:
        score += 20
    elif pb < 2:
        score += 10
    elif pb < 3:
        score += 5

    # Market cap filter: prefer mid-large cap (0-10 points)
    mv = row.get("total_mv", 0)
    if mv > 1e10:  # > 10B
        score += 10
    elif mv > 5e9:
        score += 5

    # Momentum: not crashing (0-10 points)
    pct60 = row.get("pct_60d", 0)
    if pct60 > -10:
        score += 10
    elif pct60 > -20:
        score += 5

    # YTD performance (0-10 points)
    pct_ytd = row.get("pct_ytd", 0)
    if -20 < pct_ytd < 10:
        score += 10  # not overhyped, not collapsing
    elif pct_ytd < -30:
        score -= 10  # may have fundamental issues

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------

LLM_PROMPT = """你是一位资深的美股半导体价值分析师。请根据以下股票的基本面和行情数据，评估其是否被低估，并给出0-100的"低估评分"（越高越低估）。

股票信息：
- 代码: {code}
- 名称: {name}
- 行业: {industry}
- 最新价: {price}
- 动态市盈率(PE-TTM): {pe}
- 市净率(PB): {pb}
- 总市值: {total_mv} 亿美元
- 60日涨跌幅: {pct_60d}%
- 年初至今涨跌幅: {pct_ytd}%
- 近期财务指标摘要: {financials}

请严格按以下JSON格式回复，不要输出其他内容：
{{"score": <0-100整数>, "reason": "<简要分析理由，50字以内>"}}
"""


def analyze_with_llm(
    client: OpenAI,
    model: str,
    row: pd.Series,
    info: dict,
    financials: Optional[pd.DataFrame],
) -> dict:
    """Use LLM to analyze a single stock. Returns {"score": int, "reason": str}."""
    fin_text = "暂无"
    if financials is not None and not financials.empty:
        # Take latest 4 rows of key indicators
        latest = financials.head(4)
        fin_text = latest.to_string(index=False)

    total_mv_yi = row.get("total_mv", 0) / 1e8  # 亿美元

    prompt = LLM_PROMPT.format(
        code=row["code"],
        name=row["name"],
        industry=info.get("行业", "未知"),
        price=row.get("price", "N/A"),
        pe=row.get("pe_ttm", "N/A"),
        pb=row.get("pb", "N/A"),
        total_mv=round(total_mv_yi, 2),
        pct_60d=row.get("pct_60d", "N/A"),
        pct_ytd=row.get("pct_ytd", "N/A"),
        financials=fin_text,
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        content = resp.choices[0].message.content.strip()
        # Try to parse JSON from response
        # Handle markdown code blocks
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content)
    except Exception as e:
        log.warning("LLM analysis failed for %s: %s", row["code"], e)
        return {"score": 0, "reason": f"LLM分析失败: {e}"}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def screen_undervalued_stocks(
    api_key: str,
    base_url: str,
    model: str,
    top_n: int = 50,
    quant_threshold: float = 40,
    use_llm: bool = True,
    sample_size: Optional[int] = None,
    output_dir: str = ".",
    exclude_markets: Optional[list[str]] = None,
    keep_markets: Optional[list[str]] = None,
):
    """Main screening pipeline.

    Args:
        exclude_markets: 排除的细分，如 ["设备", "存储"]。默认不排除。
        keep_markets: 仅保留的板块，如 ["主板"]。与 exclude 二选一。
    """
    # Init LLM client
    client = OpenAI(api_key=api_key, base_url=base_url) if use_llm else None

    # Step 1: Fetch real-time data
    df = fetch_realtime_quotes()
    if sample_size:
        df = df.sample(n=min(sample_size, len(df)), random_state=42)
        log.info("Sampled %d stocks for analysis", len(df))

    # Step 1b: 板块过滤
    if exclude_markets or keep_markets:
        stocks = df[["code"]].to_dict("records")
        if keep_markets:
            stocks = filter_by_market(stocks, keep=keep_markets)
            log.info("仅保留板块 %s，剩余 %d 只", keep_markets, len(stocks))
        else:
            stocks = filter_by_market(stocks, exclude=exclude_markets)
            log.info("排除板块 %s，剩余 %d 只", exclude_markets, len(stocks))
        keep_codes = {s["code"] for s in stocks}
        df = df[df["code"].isin(keep_codes)].copy()
    else:
        # 附加板块信息
        for _, row in df.iterrows():
            row_data = {"code": row["code"]}
            filter_by_market([row_data])

    # 添加板块列
    df["market"] = df["code"].apply(lambda c: get_market(c) or "未知")

    # Step 2: Compute quantitative scores
    log.info("Computing quantitative scores ...")
    df["quant_score"] = df.apply(compute_quant_score, axis=1)
    candidates = df[df["quant_score"] >= quant_threshold].copy()
    candidates = candidates.sort_values("quant_score", ascending=False)
    log.info("%d stocks passed quant filter (score >= %.0f)", len(candidates), quant_threshold)

    # Step 3: LLM analysis
    if use_llm and client:
        log.info("Running LLM analysis on %d candidates ...", len(candidates))
        llm_scores = []
        llm_reasons = []
        for idx, (_, row) in enumerate(candidates.iterrows()):
            code = row["code"]
            log.info("[%d/%d] Analyzing %s %s ...", idx + 1, len(candidates), code, row["name"])

            info = fetch_individual_info(code)
            financials = fetch_financial_indicators(code)
            result = analyze_with_llm(client, model, row, info, financials)

            llm_scores.append(result.get("score", 0))
            llm_reasons.append(result.get("reason", ""))
            time.sleep(0.5)  # rate limit

        candidates = candidates.copy()
        candidates["llm_score"] = llm_scores
        candidates["llm_reason"] = llm_reasons
        # Combined score: 50% quant + 50% llm
        candidates["final_score"] = candidates["quant_score"] * 0.5 + candidates["llm_score"] * 0.5
    else:
        candidates = candidates.copy()
        candidates["llm_score"] = 0
        candidates["llm_reason"] = "未启用LLM分析"
        candidates["final_score"] = candidates["quant_score"]

    # Step 4: Sort and output
    candidates = candidates.sort_values("final_score", ascending=False).head(top_n)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"undervalued_stocks_{timestamp}.csv")
    candidates.to_csv(output_file, index=False, encoding="utf-8-sig")
    log.info("Results saved to %s", output_file)

    # Print summary
    display_cols = ["code", "name", "market", "price", "pe_ttm", "pb", "quant_score", "llm_score", "final_score", "llm_reason"]
    avail_cols = [c for c in display_cols if c in candidates.columns]
    print("\n" + "=" * 120)
    print(f"低估股票 Top {top_n} (按低估程度排序) | 时间: {timestamp}")
    print("=" * 120)
    print(candidates[avail_cols].to_string(index=False))
    print("=" * 120)

    return candidates


def main():
    parser = argparse.ArgumentParser(description="SOXX 低估股票筛选 (量化 + LLM)")
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""), help="LLM API key")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), help="LLM API base URL")
    parser.add_argument("--model", default="gpt-4o-mini", help="LLM model name")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM analysis (quant only)")
    parser.add_argument("--top-n", type=int, default=30, help="Number of top stocks to output")
    parser.add_argument("--quant-threshold", type=float, default=40, help="Minimum quant score to enter LLM analysis")
    parser.add_argument("--sample-size", type=int, default=None, help="Sample N stocks instead of all (for testing)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory for CSV (默认: 项目根/output/)")
    parser.add_argument(
        "--exclude-markets", type=str, default=None,
        help=f"排除的板块（逗号分隔），可选: {','.join(ALL_MARKETS)}。例: --exclude-markets 设备,存储"
    )
    parser.add_argument(
        "--keep-markets", type=str, default=None,
        help=f"仅保留的板块（逗号分隔），可选: {','.join(ALL_MARKETS)}。与 --exclude-markets 二选一"
    )
    args = parser.parse_args()

    # 默认输出目录
    if args.output_dir is None:
        args.output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

    # 解析板块过滤参数
    exclude_markets = None
    keep_markets = None
    if args.keep_markets:
        keep_markets = [m.strip() for m in args.keep_markets.split(",")]
    elif args.exclude_markets:
        exclude_markets = [m.strip() for m in args.exclude_markets.split(",")]

    os.makedirs(args.output_dir, exist_ok=True)

    screen_undervalued_stocks(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        top_n=args.top_n,
        quant_threshold=args.quant_threshold,
        use_llm=not args.no_llm,
        sample_size=args.sample_size,
        output_dir=args.output_dir,
        exclude_markets=exclude_markets,
        keep_markets=keep_markets,
    )


if __name__ == "__main__":
    main()
