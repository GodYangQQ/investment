#!/usr/bin/env python3
"""
美股半导体细分行业判断
根据 SOXX 成分股 ticker 映射到细分赛道（替代原 A 股主板/创业板/科创板）。

用法:
    from core.filters.market_filter import get_market, filter_by_market, ALL_MARKETS
    board = get_market("NVDA")   # -> "GPU/AI加速"
"""

from typing import Optional

ALL_MARKETS = [
    "GPU/AI加速", "存储", "设备", "代工", "模拟/MCU",
    "连接/高速接口", "电源管理", "移动SoC/无线", "CPU/逻辑", "其他",
]

# ticker -> 细分
TICKER_INDUSTRY = {
    "NVDA": "GPU/AI加速",
    "AMD": "GPU/AI加速",
    "AVGO": "连接/高速接口",
    "INTC": "CPU/逻辑",
    "MU": "存储",
    "SKHY": "存储",
    "TSM": "代工",
    "ASX": "代工",
    "UMC": "代工",
    "TSEM": "代工",
    "MRVL": "连接/高速接口",
    "ALAB": "连接/高速接口",
    "CRDO": "连接/高速接口",
    "QCOM": "移动SoC/无线",
    "ADI": "模拟/MCU",
    "TXN": "模拟/MCU",
    "NXPI": "模拟/MCU",
    "MCHP": "模拟/MCU",
    "ON": "模拟/MCU",
    "STM": "模拟/MCU",
    "KLAC": "设备",
    "AMAT": "设备",
    "LRCX": "设备",
    "TER": "设备",
    "ASML": "设备",
    "ENTG": "设备",
    "MPWR": "电源管理",
    "ARM": "CPU/逻辑",
    "CBRS": "GPU/AI加速",
    "MTSI": "连接/高速接口",
    "SNPS": "设备",
    "CDNS": "设备",
}

MARKET_LABELS = {
    "gpu": "GPU/AI加速",
    "memory": "存储",
    "equip": "设备",
    "foundry": "代工",
    "analog": "模拟/MCU",
    "connect": "连接/高速接口",
    "power": "电源管理",
    "mobile": "移动SoC/无线",
    "cpu": "CPU/逻辑",
    "other": "其他",
}
MARKET_TO_LABEL = {v: k for k, v in MARKET_LABELS.items()}

INDUSTRY_ALIAS = {
    "gpu": "GPU/AI加速", "ai": "GPU/AI加速", "nvidia": "GPU/AI加速",
    "memory": "存储", "dram": "存储", "nand": "存储", "micron": "存储", "hynix": "存储",
    "equipment": "设备", "lam": "设备", "kla": "设备", "asml": "设备", "applied": "设备",
    "foundry": "代工", "tsmc": "代工", "tower": "代工",
    "analog": "模拟/MCU", "mcu": "模拟/MCU", "nxp": "模拟/MCU", "microchip": "模拟/MCU",
    "connect": "连接/高速接口", "broadcom": "连接/高速接口", "marvell": "连接/高速接口",
    "astera": "连接/高速接口", "credo": "连接/高速接口",
    "power": "电源管理", "monolithic": "电源管理",
    "qualcomm": "移动SoC/无线", "qcom": "移动SoC/无线",
    "intel": "CPU/逻辑", "arm": "CPU/逻辑",
}


def get_market(code: str) -> Optional[str]:
    """返回细分赛道中文名。"""
    code = str(code).strip().upper()
    if code in TICKER_INDUSTRY:
        return TICKER_INDUSTRY[code]
    return "其他"


def get_market_label(code: str) -> Optional[str]:
    board = get_market(code)
    return MARKET_TO_LABEL.get(board, "other")


def resolve_industry(name: str, code: str = "") -> str:
    if code:
        mapped = TICKER_INDUSTRY.get(str(code).strip().upper())
        if mapped:
            return mapped
    combined = f"{name} {code}".lower()
    for kw, sector in INDUSTRY_ALIAS.items():
        if kw in combined:
            return sector
    return "其他"


def filter_by_market(
    stocks: list[dict],
    keep: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> list[dict]:
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
    if keep is None and exclude is None:
        return stocks if stocks and "market" in stocks[0] else [
            {**s, "market": get_market(s.get("code", "")) or "未知"} for s in stocks
        ]
    return result


def filter_codes_by_market(
    codes: list[str],
    keep: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> list[str]:
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


if __name__ == "__main__":
    for c in ["NVDA", "MU", "AMAT", "TSM", "TXN", "ALAB", "INTC", "MPWR"]:
        print(f"{c:<6} {get_market(c)}")
