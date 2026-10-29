# 美股半导体分析模板（SOXX 前20）

> 研究宇宙 = SOXX 前 20。数据截止 **T-1 收盘**（美东 16:00）。零未来函数。
> 命令在项目根目录执行。标的用 ticker：`NVDA` `TSM` `AMAT`。

---

## 0. 先跑脚本，再写结论

```bash
python core/datasets/universe.py                         # 刷新池
python core/quant_score.py NVDA                 # 单股打分 + SOXX 风控
python core/quant_score.py NVDA --cost 120 --shares 50 --buy-date 2026-05-10
python core/stock_strategy.py NVDA --kline-days 250
python apps/cli/rank.py --top 10
python apps/cli/flow.py --days 5
python core/sector_heat.py
python core/sector_heat.py GPU/AI加速
```

脚本已算的不要手算：量化分、PE/PB、均线、量比、供需⭐、SOXX 仓位上限。
AI 必须补：消息面四层、供应链卡点、形态、买点、止盈止损、概念⭐。

---

## 1. 消息面四层 [必填]

| 层 | 搜什么 | 来源 |
|---|---|---|
| ① 公司 | 业绩指引、回购、客户、产能、诉讼、高管增减持 | Yahoo `{TICKER}` news、SEC 8-K/10-Q、IR |
| ② 行业 | 存储价、晶圆开工、设备订单、AI 资本开支、手机/PC | 公司电话会、Gartner/WSTS 摘要、同业 10-Q |
| ③ 宏观 | FOMC、实际利率、美元、出口管制、关税 | Fed、BIS 实体清单、USTR |
| ④ SOXX 市场 | SOXX vs QQQ、ETF 资金、高 beta 是否一起砸 | `quant_score` 风控块、Yahoo SOXX |

每层填表：日期 | 事件 | 影响方向(✅/⚠️/❌) | 是否已计价。

**半导体特例（不是 A 股“利好出货”那套）**：
- 业绩 beat + 指引 raise 且盘后大涨：趋势延续，不因为“利好”就减仓。
- 业绩 beat + 指引平 + 盘后大跌：预期透支，按跌破买点处理。
- 出口管制/实体清单：先判断是份额转移（TSM/AMAT 受益）还是需求消灭。
- NVDA 电话会资本开支数字，是全池最强的需求领先指标。

---

## 2. 供需三层 [必填]

脚本 `supply_demand.py` 给出财务硬指标（营收加速度、利润质量、PEG、毛利率、存货周转、ROE）。AI 必须再补：

**需求**：谁在买、ASP、订单/backlog、下游 capex（云厂商、手机、汽车）。
**供给**：产能（晶圆/封装/设备交期）、存货天数、竞争对手扩产。
**价格**：DRAM/NAND 现货、晶圆代工报价、GPU 是否还在 allocation。

存储股（MU/SKHY）看现货价和存货；设备股（AMAT/LRCX/KLAC）看 WFE 和客户 capex；设计股看设计赢单和节点迁移。

---

## 3. 供应链卡点

沿 SOXX 池往上/往下看一跳：
- 设计：NVDA AMD AVGO QCOM MRVL
- 代工：TSM INTC（IFS）
- 设备：AMAT LRCX KLAC TER
- 存储：MU SKHY
- 连接/电源：ALAB CRDO MPWR

问：缺的是产能、先进封装、HBM、还是客户 capex？卡点在池内哪只上？

---

## 4. 技术形态

用 `stock_strategy.py` 的近 180 日高低点判断：趋势通道、杯柄、旗形、双底、头肩。美股无涨停，用：
- 单日 ±8% 视为事件日
- 连续 5 日大阳 = 高潮，不加仓
- 缺口：财报缺口常不补，普通缺口按支撑/阻力

---

## 5. 买入 / 仓位 / 止盈止损

**买点**（二选一写死价格）：
- 突破买入：明确价 + 放量条件
- 回踩买入：MA20 / 前高 / 斐波那契 38.2–50%

**仓位**：先看 SOXX 风控上限，再看个股可买度与基本面⭐。弱势市只留核心池最高分。

**止损**：ATR × 1.5 或结构位下沿，写具体美元价。
**止盈**：A+ 回撤 10%；A 回撤 8%；B 回撤 6%。趋势单用移动止盈，不设死目标。

等级：基本面⭐≥4 且概念⭐≥4 → A+；基本面⭐≥4 → A；其余 B。概念⭐只能 AI 标，脚本不算。

---

## 6. 输出结构（个股）

1. SOXX 风控（脚本原文）
2. 量化评分表（脚本原文）
3. 消息面四层表 + 综合方向
4. 供需三层 + 脚本供需⭐
5. 供应链一句话
6. 形态与关键价
7. 操作：买点 / 仓位 / 止损 / 止盈 / 不买的否决条件

## 7. 输出结构（全池排名）

对 `apps/cli/rank.py` Top N：只点评可买度≥关注的票。每只 8–12 行：为什么排这、最大风险、买点。最后给 1 张仓位建议表（受 SOXX 上限约束）。

---

## 8. 数据源（不要用 A 股站点）

- Yahoo Finance quote/news/financials
- SEC EDGAR（10-K/10-Q/8-K）
- 公司 IR + 财报电话会稿
- Seeking Alpha / Bloomberg 标题（交叉验证，不单信）
- iShares SOXX holdings CSV

不要用：东方财富、同花顺、雪球、新浪财经、北向资金、涨停板。
