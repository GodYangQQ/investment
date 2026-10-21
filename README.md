# 美股半导体量化投资系统（SOXX 前20）

研究宇宙固定为 **iShares Semiconductor ETF (SOXX) 前 20 大持仓**。数据主走 Nasdaq 公开 API（行情/K线/财报），Yahoo (`yfinance`) 作备用，打分、供需、回测、资金流都在这 20 只里转。

美股没有涨停、北向资金、印花税。资金流用成交额 × 涨跌方向做代理；大盘风控默认看 **SOXX** 相对 MA60/MA200。

## 快速开始

```bash
pip install -r requirements.txt
python core/universe.py                  # 刷新 SOXX 前20 → data/my_pool.csv
python core/quant_score.py NVDA          # 单股打分 + SOXX 仓位上限
python run/rank.py --top 10              # 池内排名
python core/stock_strategy.py AMD        # 结构化个股数据（给 LLM）
```

| 你想做什么 | 命令 |
|---|---|
| 刷新标的池 | `python core/universe.py` |
| 给某只打分 | `python core/quant_score.py NVDA --cost 120 --shares 50 --buy-date 2026-05-10` |
| 全池排名 | `python run/rank.py` |
| 资金流向 | `python run/flow.py --days 5` |
| 细分景气度 | `python core/sector_heat.py` |
| 回测 | `python run/backtest.py --start 2025-01-01` |
| 特征矩阵 | `python ml/build_features.py` |
| 世界大事件→标的 | `python run/events.py --top 20` |

## 股票池

默认 20 只来自 iShares SOXX 最新权重（失败回退 `core/universe.py` 内置快照，截至 2026-09-28）：

INTC / AMD / MU / NVDA / AVGO / MRVL / QCOM / ADI / KLAC / TXN / AMAT / LRCX / MPWR / TER / SKHY / TSM / NXPI / ALAB / MCHP / CRDO

自选与持仓写在 `data/my_pool.csv`：

```
代码,名称,层级,成本价,持仓量,买入日期
NVDA,NVIDIA,核心,120.00,50,2026-05-15
AMD,AMD,观察,,,
```

SKHY（海力士 ADR）若 Yahoo 无数据，自动回退 `000660.KS`。

## 量化模型（4 因子，满分 100）

| 因子 | 权重 | 内容 |
|---|---|---|
| 趋势+位置 | 35 | MA20/MA60、价格在 20/60 日区间位置 |
| 量价+Price Action | 30 | 量比、实体、影线、缺口 |
| 波动率 | 15 | ATR、近端波动 |
| 斐波那契/结构 | 20 | 回撤位置、局部高低点 |

可买度：≥78 强烈推荐 / 68–77 推荐 / 55–67 关注 / 40–54 偏弱 / <40 回避。

仓位上限看 SOXX vs MA60/MA200：强势 100% / 震荡 50% / 弱势 30%。

## 数据源

| 用途 | 来源 |
|---|---|
| 行情 / K线 / 财报 | Nasdaq API（主）/ yfinance（备） |
| SOXX 持仓权重 | iShares 官网 CSV |
| 资金流 | 日线成交额 × 涨跌方向（非 Level 2） |
| 世界事件新闻 | Google News RSS（中英）+ USGS 地震 |
| 新闻（给 LLM） | Yahoo / SEC EDGAR / 公司 IR / Seeking Alpha |

交易时段：美东 09:30–16:00。分析默认用 **T-1 收盘**，盘中可叠加实时价。

## 世界大事件 → 逻辑链 → 候选标的

`python run/events.py` 扫描当前全球重大事件（地震/飓风/洪水/干旱/野火/高温/疫情/战争/制裁/航运/能源/网攻/联储/AI 资本开支），按 `data/event_chains.json` 的逻辑链映射到可能受益/受损的美股标的。

- 知识库 `data/event_chains.json` 可自行增改：类别 → 关键词/地区 → 逻辑 → 多/空标的。
- 输出：事件热度概览 + 候选标的（含方向、分数、逻辑、关联事件），并写 `output/events_YYYYMMDD.csv`。
- 常用参数：`--category war_geopolitics` 只看某类；`--min-mag 6` 提高地震门槛；`--quotes` 补实时行情；`--no-zh` 仅英文。

> 方向仅为逻辑推演（多=受益 / 空=受损），须结合 `quant_score` 与基本面二次验证，不是买卖建议。

## A股短线炒作 pipeline（大事件 → 模块 → 前3龙头）

与上面美股 SOXX 系统并列的一条独立链路，只做**题材炒作**（短线情绪），不做价值。

```bash
python run/cn_pick.py                     # 全量：炒作模块 + 每模块前3龙头
python run/cn_pick.py --category plague   # 只看鼠疫/烈性传染病链
python run/cn_pick.py --module 抗生素      # 直接查某模块
python run/cn_pick.py --timespan 30d --top-modules 12
```

**两级 pipeline**：
1. **先找模块** —— 扫描全球权威事件 → 按 `data/cn_event_chains.json` 的逻辑链映射到 A股题材模块（概念板块），模块按"事件热度 × 权威度"排序。
2. **再选前3** —— 每个模块用腾讯行情/K线对候选股按**炒作强度**（涨幅、量比、连板、5/20日动量）实时排序，取前3。

**事件源（全球权威，非散户向）**：
| 来源 | 说明 |
|---|---|
| WHO / CDC / FAO RSS | 官方机构（疫情/食安/粮农） |
| USGS | 全球显著地震（M5.5+） |
| GDELT 2.0 | 全球 65 语种事件库（失败自动熔断） |
| Google News（限权威域名） | Reuters/AP/BBC/彭博 等兜底 |
| Bluesky 公共 API | WHO/UN/NOAA 等"大佬发声"（能连则用） |

只保留**全球级重大事件**（鼠疫/非典/霍乱/埃博拉/厄尔尼诺/地震/战争/制裁…），用 `STRONG_TERMS` 显著性门槛过滤日常噪音。

**知识库** `data/cn_event_chains.json`：类别 → 关键词 → 炒作链逻辑 → 模块(board_kw) → 候选龙头。示例：`鼠疫 → 抗生素 → 600812 华北制药 / 600789 鲁抗医药 / 600597 东北制药`。

> 仅研究用途；短线追涨风险极高，注意情绪退潮与量价背离。

## 目录

```
core/     美股数据/打分/供需/资金流/细分 + 全球事件/A股炒作
run/      rank / flow / screen / backtest / events / cn_pick
ml/       特征矩阵
data/     my_pool.csv、event_chains.json、cn_event_chains.json
docs/     分析模板、仓位风控
scripts/  持仓看板、RSRS、监控
```

## 分析模板

深度个股分析走 `docs/PROMPT_TEMPLATE.md`：消息面四层（公司 / 半导体行业 / 宏观Fed / SOXX 市场）、供需三层、供应链卡点、形态、止盈止损。
