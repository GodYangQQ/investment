#!/usr/bin/env python3
"""
A股市场板块判断工具
根据股票代码前缀自动识别所属板块。

板块划分规则：
  - 主板（沪）:  600xxx, 601xxx, 603xxx, 605xxx
  - 主板（深）:  000xxx, 001xxx, 002xxx, 003xxx
  - 创业板:      300xxx, 301xxx
  - 科创板:      688xxx
  - 北交所:      4xxxxx, 83xxxx, 87xxxx

用法：
    from market_filter import get_market, filter_by_market, ALL_MARKETS

    board = get_market("300750")   # -> "创业板"
    board = get_market("600519")   # -> "主板"

    codes = ["600519", "300750", "688981"]
    filtered = filter_by_market(codes, keep=["主板", "创业板"])  # 排除科创板
"""

from typing import Optional

# 所有支持的板块
ALL_MARKETS = ["主板", "创业板", "科创板", "北交所"]

# 板块label -> 中文名
MARKET_LABELS = {
    "main": "主板",
    "gem": "创业板",      # Growth Enterprise Market
    "star": "科创板",     # STAR Market
    "bse": "北交所",      # Beijing Stock Exchange
}

# 板块中文名 -> label
MARKET_TO_LABEL = {v: k for k, v in MARKET_LABELS.items()}


def get_market(code: str) -> Optional[str]:
    """
    根据股票代码判断所属板块。

    Args:
        code: 股票代码，如 "600519"、"300750"

    Returns:
        板块中文名: "主板" / "创业板" / "科创板" / "北交所"
        无法判断返回 None
    """
    code = str(code).strip().zfill(6)  # 统一6位

    # 科创板: 688xxx
    if code.startswith("688"):
        return "科创板"

    # 创业板: 300xxx, 301xxx
    if code.startswith(("300", "301")):
        return "创业板"

    # 北交所: 4xxxxx, 83xxxx, 87xxxx
    if code.startswith(("4", "83", "87")):
        return "北交所"

    # 深市主板: 000xxx, 001xxx, 002xxx, 003xxx
    if code.startswith(("000", "001", "002", "003")):
        return "主板"

    # 沪市主板: 60xxxx
    if code.startswith("60"):
        return "主板"

    return None


def get_market_label(code: str) -> Optional[str]:
    """返回板块英文label: main/gem/star/bse"""
    board = get_market(code)
    return MARKET_TO_LABEL.get(board)


def filter_by_market(
    stocks: list[dict],
    keep: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> list[dict]:
    """
    按板块过滤股票列表。支持两种模式（二选一）：
    1. keep:  只保留指定板块（如 keep=["主板", "北交所"]）
    2. exclude: 排除指定板块（如 exclude=["创业板", "科创板"]）

    Args:
        stocks: 股票字典列表，每条需含 "code" 键
        keep: 要保留的板块列表
        exclude: 要排除的板块列表

    Returns:
        过滤后的股票列表，每条增加 "market" (板块名) 字段
    """
    if keep is None and exclude is None:
        # 不过滤，仅附加板块信息
        for s in stocks:
            s["market"] = get_market(s.get("code", "")) or "未知"
        return stocks

    result = []
    for s in stocks:
        board = get_market(s.get("code", ""))
        s["market"] = board or "未知"

        if keep is not None:
            if board in keep:
                result.append(s)
        elif exclude is not None:
            if board not in exclude:
                result.append(s)
        else:
            result.append(s)

    return result


def filter_codes_by_market(
    codes: list[str],
    keep: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> list[str]:
    """
    按板块过滤纯代码列表。

    Args:
        codes: 股票代码列表
        keep: 要保留的板块
        exclude: 要排除的板块

    Returns:
        过滤后的代码列表
    """
    result = []
    for code in codes:
        board = get_market(code)
        if keep is not None:
            if board in keep:
                result.append(code)
        elif exclude is not None:
            if board not in exclude:
                result.append(code)
        else:
            result.append(code)
    return result


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    # 快速测试
    test_codes = [
        "600519", "000001", "002475",  # 主板
        "300750", "301666",            # 创业板
        "688981", "688072",            # 科创板
        "430047", "830000", "871981",  # 北交所
    ]
    print("代码       板块")
    print("-" * 20)
    for c in test_codes:
        print(f"{c:<10} {get_market(c)}")

    print("\n--- 排除创业板+科创板 ---")
    filtered = filter_codes_by_market(
        test_codes, exclude=["创业板", "科创板"]
    )
    print(filtered)

    print("\n--- 只保留主板 ---")
    filtered = filter_codes_by_market(
        test_codes, keep=["主板"]
    )
    print(filtered)

    print("\n--- 附加板块信息（不过滤） ---")
    stocks = [{"code": c, "name": c} for c in test_codes]
    for s in filter_by_market(stocks):
        print(f"  {s['code']} -> {s.get('market')}")
