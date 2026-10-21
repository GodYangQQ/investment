# 半导体仓位评估（SOXX）

> 风控基准默认 SOXX，可切 QQQ。美股无涨停、无北向资金。

## 一、估值

| 指标 | 用法 | 阈值黄 | 阈值红 |
|---|---|---|---|
| SOXX / 成分股中位 PE | Yahoo trailingPE | 高于近 5 年 70% 分位 | 90% 分位 |
| SOX vs QQQ 相对 PE | 半导体溢价 | 溢价扩张过快 | 溢价历史极值 + 盈利下修 |
| 股债性价比 | 盈利收益率 vs 10Y | ERP < 3% | ERP < 2% |

数据：yfinance `info.trailingPE`；美债 10Y 用 `^TNX` 或 FRED。

## 二、情绪 / 资金

| 指标 | 黄 | 红 |
|---|---|---|
| SOXX 相对 QQQ 20 日超额 | 连续走弱 | 跌破且放量 |
| ETF 成交额 | 天量后缩量 | 价新高量萎缩 |
| 高 beta 票（NVDA/AMD/ALAB/CRDO）同步大跌 | 内部扩散 | 设备+存储一起砸 |

资金流模块用 dollar volume 代理，不是 A 股主力净流入。

## 三、技术

| 指标 | 规则 |
|---|---|
| SOXX vs MA60 | 站上 → 仓位上限 100%；跌破 → 50% |
| SOXX vs MA200 | 跌破 → 仓位上限 30%，只留最高分 1–2 只 |
| 周线 MACD 顶背离 | 减仓，不新开趋势仓 |

脚本：`python core/quant_score.py NVDA` 会先打印 SOXX 风控块。
