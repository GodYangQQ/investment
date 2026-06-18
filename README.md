# A股量化投资系统 — 用户手册

## 开始之前：如何用 AI 助手操作本项目

你不需要手动敲命令。把 `docs/PROMPT_TEMPLATE.md` 发给任意 AI 编程助手（Copilot/Cursor/Claude Code/CodeBuddy 等），然后用自然语言描述你想做什么即可。

### 第一步：把 Prompt 模板喂给 AI

将 `docs/PROMPT_TEMPLATE.md` 的内容提供给 AI 助手，或直接说：

```
阅读 docs/PROMPT_TEMPLATE.md，了解这个项目的用法和你的工作流
```

AI 读完后会按照模板中定义的工作流（数据质量门禁 → 选股 → 深度分析 → 交易建议）严格执行。

### 第二步：用自然语言下指令

以下操作只需用中文描述，AI 会自动翻译成对应的命令并执行：

| 你想做什么 | 直接这样说 |
|-----------|-----------|
| 全自动选股 | `帮我选股` 或 `看看今天哪些值得买` |
| **深度学习选股** | `用ML模型预测一下Top30` |
| **训练ML模型** | `训练一下深度学习模型，50轮` |
| 给某只股票打分 | `帮我给600519打个分，成本价1680，100股，5月10号买的` |
| 跑全市场排名 | `跑一次全市场排名，取Top50` |
| 分析股票池 | `分析一下我的股票池` 或 `看看持仓哪些该动` |
| 深度分析某股 | `帮我分析一下600519` |
| 回测策略 | `回测一下多因子策略，从2024年1月1号开始` |
| 看资金流向 | `今天的资金流向报告跑一下，只看主板` |
| 验证策略改动 | `我把quant_score.py的RSI权重改了，帮我回测验证` |
| **更新持仓** | `更新持仓` → 自动OCR识别 持仓.jpg → 覆盖my_pool.csv → 刷新JSON |
| **打开持仓看板** | `打开持仓看板` → 浏览器打开 dashboards/positions.html |
| **🆕 战略排名** | `跑一下战略排名` 或 `看看哪些有卡脖子逻辑` → 基于国产替代+稀缺性独立评分 |

### 第三步：让 AI 帮你读结果

执行完成后，直接让 AI 帮你分析输出文件：

```
帮我看看 output/quant_top100.csv 前10名是哪些
```

> **提示**：全市场扫描类操作（排名、回测、资金流向）通常需要几分钟到十几分钟。

---

## 1. 安装

```bash
pip install pandas requests numpy akshare torch scikit-learn pyarrow
```

> 含深度学习所需依赖（PyTorch）。

> 仅4个依赖，无需数据库或聚宽平台。

---

## 2. 五大核心功能（快速上手）

### 2.1 单只股票打分

看看某只股票现在值多少分（满分100）：

```bash
# 基础打分
python core/quant_score.py 600519

# 带持仓成本（实盘辅助）
python core/quant_score.py 600519 --cost 1680.00 --shares 100 --buy-date 2026-05-10

# 输出JSON
python core/quant_score.py 600519 --output result.json
```

**输出**：各因子得分明细 + 总分 + 持仓盈亏修正。

---

### 2.2 全市场排名

遍历全部A股（约5000只），按量化模型打分排序，输出Top N：

```bash
# 默认 Top100
python run/rank.py

# 只取 Top50，20线程加速
python run/rank.py --top 50 --workers 20

# 断点续跑（网络中断后继续）
python run/rank.py --resume

# 排除创业板+科创板
python run/rank.py --exclude-markets 创业板,科创板
```

**耗时**：10线程约8-20分钟。结果输出到 `output/quant_top100.csv`。

---

### 2.3 策略回测

用历史数据验证策略表现，输出收益曲线和绩效指标：

```bash
# 多因子轮动策略（全市场选股）
python run/backtest.py

# 自定义参数
python run/backtest.py --start 2024-01-01 --top 10 --rebalance 5

# 趋势追涨策略（AI算力产业链）
python run/trend_backtest.py

# 自定义池
python run/trend_backtest.py --no-ai-pool --pool data/my_pool.csv
```

**输出**：
- 终端：年化收益、夏普比率、最大回撤、胜率、换手率
- 文件：`output/backtest/backtest_nav_*.csv`（逐日净值）、`*_trades_*.csv`（逐笔交易）

---

### 2.4 资金流向日报

每天收盘后跑一次，看主力资金在买什么、卖什么：

```bash
# 当日全市场 Top50
python run/flow.py

# 近3日累计，只看主板
python run/flow.py --top 100 --days 3 --market 主板
```

**输出**（`output/`目录）：
- `money_flow_top50_*.csv` — 净流入排名
- `money_flow_bottom50_*.csv` — 净流出排名（风险预警）
- `money_flow_consecutive_*.csv` — 连续流入/流出个股
- `money_flow_market_summary_*.csv` — 板块汇总

---

### 2.5 单只股票资金流向

```bash
# 查看贵州茅台近10日资金动向
python core/money_flow.py 600519

# 全市场排名
python core/money_flow.py --top 50 --days 5
```

---

### 2.6 深度学习预测（ML Pipeline）🆕

用神经网络预测股票未来5日超额收益概率，按概率排序选股：

```bash
# 首次使用：构建特征矩阵（约15-30分钟，只需跑一次）
python ml/build_features.py

# 训练模型（GPU推荐，CPU也可）
python ml/train.py --epochs 50

# 预测选股（使用最新模型）
python ml/predict.py --top 30

# 回测验证（模拟历史逐日选股效果）
python ml/predict.py --backtest --start 2025-04-01
```

**模型架构**：1D Conv (序列压缩) + MLP (分类头)  
**输入**：每只股票过去180天 × 34个技术指标  
**输出**：P(未来5日超额收益 > 0)  
**评估**：测试集AUC + 分层收益验证

---

## 3. 项目结构

```
investment/
├── core/                    ← 核心引擎（不要直接运行，被其他脚本调用）
│   ├── stock_strategy.py        数据获取 + 技术指标计算
│   ├── quant_score.py           多因子打分引擎 ★
│   ├── trend_strategy.py        趋势追涨策略 + AI算力股票池
│   ├── fundamental_filter.py    基本面过滤（PE/ROE/毛利率）
│   ├── market_filter.py         板块判断（主板/创业板/科创板/北交所）
│   ├── money_flow.py            主力资金流向分析
│   ├── sector_heat.py           板块景气度评估（多时间框架）🆕
│   └── supply_demand.py         供需评估引擎（6项硬指标）🆕
│
├── run/                     ← 可执行脚本（每日运行入口）
│   ├── rank.py                  全市场量化排名 ★
│   ├── backtest.py              多因子轮动回测
│   ├── trend_backtest.py        趋势追涨回测
│   ├── flow.py                  每日资金流向报告
│   └── screen.py                低估股票筛选（集成LLM分析）
│
├── data/                    ← 数据文件
│   ├── my_pool.csv              自选股票池
│   ├── all_stocks_score_intermediate.csv  全市场打分中间结果
│   └── 板块/                    38个行业板块CSV
│
├── output/                  ← 所有输出（排名/回测/资金流向）
│   └── backtest/                回测净值 + 交易明细
│
├── experiments/             ← 参考策略（聚宽平台代码，供借鉴）
├── dashboards/              ← HTML可视化看板
│   ├── positions.html           持仓监控仪表板（实时行情+止盈止损）
│   ├── holdings_data.json       持仓量化数据（由脚本自动生成）
│   └── dashboard-v50.html       AI算力产业链全景看板
├── scripts/                 ← 工具脚本
│   ├── update_holdings_data.py  持仓数据生成器（my_pool.csv → holdings_data.json）
│   └── monitor_ambush.py        策略埋点实时监控终端
├── ml/                      ← 深度学习训练+预测 🆕
│   ├── build_features.py        特征矩阵构建
│   ├── dataset.py               动态序列切片 Dataset
│   ├── train.py                 MLP 训练+评估
│   └── predict.py               预测+选股
└── docs/                    ← Prompt模板
```

---

## 4. 常用工作流

### 场景A：日常选股

```
周一~周五收盘后：
  1. python run/rank.py --top 100          → 拿到全市场Top100
  2. 查看 output/quant_top100.csv          → 重点关注排名上升的
  3. python run/flow.py                     → 确认主力资金方向是否一致
  4. python core/quant_score.py 600xxx      → 对感兴趣的个股逐只复核
  5. 逐只检查景气度（必做）：结合消息面搜索+板块资金流+涨停板数量，
     景气度<40 的标的直接排除，不进入持仓决策
```

### 场景B：验证策略想法

```
  1. 修改 core/quant_score.py 中的因子权重或公式
  2. python run/backtest.py --start 2023-01-01   → 回测验证
  3. 对比 output/backtest/ 中的净值曲线和年化收益
  4. 效果满意 → 用新参数跑 rank.py 选股
```

### 场景C：实盘持仓评估

```
  1. 保存券商APP持仓截图 → 覆盖项目根目录 持仓.jpg
  2. 对AI说："更新持仓"
     → 自动OCR识别持仓.jpg → 更新 data/my_pool.csv → 运行 update_holdings_data.py
  3. 浏览器打开 dashboards/positions.html 查看实时看板
```

> 📌 **约定**：持仓截图固定为项目根目录 `持仓.jpg`，每次截图覆盖即可。Agent 找不到文件时会提示"请先将持仓截图保存为 持仓.jpg"。

---

## 5. 策略逻辑（技术细节）

### 5.1 多因子打分模型（quant_score.py）

每只股票0-100分，完全由机器计算，零人工干预：

| 因子 | 满分 | 计算依据 |
|------|:----:|----------|
| 趋势 | 25 | MA5/MA10/MA20排列间距 + 价格vs MA60位置 |
| 位置 | 25 | 近期高低点位置 + 布林带位置（均值回归判断） |
| 量价 | 20 | 量比 + OBV趋势 + CMF资金流 + 量价相关性 |
| RSI | 15 | RSI值 + RSI斜率 + RSI曲率（加速度） |
| 波动率 | 15 | ATR波动率在历史中的分位数 |
| 额外 | ±13 | MACD金叉/死叉、均线突破/跌破 |
| **成本修正** | ±5 | 持仓盈亏（仅实盘，回测不启用） |
| **景气度** | **门禁** | **硬性前置条件：景气度<40 → 一票否决，量化分再高也不买** |

> **景气度评估**：由 `core/sector_heat.py` 和 AI 消息面搜索共同完成。六维评估（板块涨幅排名/资金净流入/涨停板数量/连板龙头/催化剂/20日动量），满分100。<40 极低景气时直接否决。

### 5.2 趋势追涨策略（trend_strategy.py）

- **股票池**：内置AI算力产业链约60+只（GPU/光模块/PCB/液冷/存储/IDC等）
- **评分体系**：量比(25%) + 量比加速度(30%) + 资金流(25%) + 动量(20%)
- **选股**：排名11-30中选5只（放弃Top10，避免极端追高）
- **仓位**：情绪强→满仓 / 一般→半仓 / 退潮→空仓

### 5.3 风控体系（backtest.py）

| 类型 | 参数 | 说明 |
|------|------|------|
| 固定止损 | -8% | 跌破成本价8%无条件卖出 |
| 回撤止盈 | -10% | 从最高点回撤10%止盈 |
| 保本止损 | 盈利>3%后回撤到成本 | 保护本金 |
| 保利止损 | 盈利5-10%后回撤到+5% | 锁定利润 |
| 均线止损 | 跌破MA5/MA20 | 趋势破坏信号 |
| 买入次日观察 | 1天 | 买入次日不触发止损 |

### 5.4 基本面过滤（fundamental_filter.py）

**快速过滤**（无网络，基于已有行情数据）：
- PE < 0 → 排除（亏损股）
- PE > 200 → 排除（估值离谱）

**深度过滤**（需akshare，仅对Top N执行）：
- ROE ≥ 8%
- 毛利率 ≥ 15%
- 经营现金流/净利润 > 0
- 资产负债率 < 70%

### 5.5 景气度评估（硬性门禁）🆕

> **量化分高 ≠ 现在可以买。板块没热之前，再好的逻辑也会被市场无视。**

景气度六维评估（100分制），由 `core/sector_heat.py` + AI消息面搜索共同完成：

| 指标 | 权重 | 评分标准 |
|------|:--:|------|
| 板块5日涨幅排名 | **25%** | Top5→5分, Top10→4分, Top15→3分, Top20→2分, 其余→1分 |
| 板块资金净流入 | **25%** | 连续5日净流入→5分, 3日→4分, 1日→3分, 流出→1分 |
| 涨停板数量趋势 | **20%** | ≥3只涨停且增多→5分, 1-2只→3分, 0只→1分 |
| 连板龙头存在 | **15%** | 有3连板以上→5分, 有涨停无连板→3分, 无→1分 |
| 近期催化剂 | **10%** | 有重大政策/产业新闻→5分, 有行业会议→3分, 无→1分 |
| 板块20日动量 | **5%** | 20日涨幅>10%→5分, 5-10%→3分, 0-5%→2分, 负→1分 |

| 总分 | 状态 | 操作 |
|------|:--:|------|
| ≥ 75 | 🔥 高景气 | 正常仓位买入 |
| 60-74 | 中等景气 | 半仓买入 |
| 40-59 | 低景气 | 仅观察不买，等催化剂 |
| **< 40** | **极低景气** | **一票否决。量化分再高也不买** |

> 使用方式：
> ```bash
> # 查某只股票所属板块的景气度
> python core/sector_heat.py --stock 603893
> 
> # 查指定板块景气度
> python core/sector_heat.py 半导体
> 
> # 全板块排名
> python core/sector_heat.py
> 
> # 从量化评分JSON聚合
> python core/sector_heat.py --json output/pool_scores_20260530.json
> ```

### 5.6 资金流向分析（money_flow.py）

- **数据源**：akshare 个股资金流向（逐日主力/超大单/大单/中单/小单）
- **主力净流入** = 超大单净流入 + 大单净流入
- **维度**：个股/全市场排名/板块汇总/连续N日统计

### 5.7 供需评估（三层体系）🆕

> **股价上涨最底层的驱动力是供需。技术面和消息面只是供需的表征。**

#### 第一层：脚本自动计算（6项硬指标，零主观）

由 `core/supply_demand.py` 基于财报数据提取，已集成到 `quant_score.py` 输出：

| 指标 | 含义 | 数据源 |
|------|------|--------|
| D1.营收加速度 | 季度营收YoY增速趋势 | 同花顺财报 |
| D2.利润质量 | 净利增速/营收增速比(>1.5=规模效应) | 同花顺财报 |
| D3.PEG | PE÷净利增速(<1=低估) | 财报+行情 |
| S1.毛利率趋势 | 毛利率变化方向(上升=供给偏紧) | 同花顺财报 |
| S2.存货周转 | 周转加速=供不应求 | 同花顺财报 |
| S3.ROE水平 | 资本回报效率 | 同花顺财报 |

使用方式（独立运行）：
```bash
python core/supply_demand.py 603893 --pe 63.77
```

> 已集成到 `quant_score.py`，打分时自动输出供需评估章节。

#### 第二层：消息面扫描（AI搜索）

AI 必须搜索并提取：订单/合同、产品放量、产能/供给、行业供需四类信号。

#### 第三层：综合研判（AI分析）

判断需求是结构性还是周期性、竞争优势能否持续、产品生命周期衔接。

> 详见 `docs/PROMPT_TEMPLATE.md` 中的"供需评估"和"供应链位置分析"章节。

### 5.8 供应链位置分析（产业链视角）🆕

> **一只股票的价值不仅取决于自身财务数据，还取决于它在产业链中的位置。**

AI 分析时必须回答：
1. 公司在产业链的哪一环？（上游/中游/下游）
2. 关键供应商和客户是谁？是否有集中依赖风险？
3. 产业链利润当前在往哪个环节集中？
4. 议价能力：对上游强/中/弱 | 对下游强/中/弱

> 例：圣泉集团 → 生益科技 → 沪电股份 → 华为/英伟达，形成"树脂→CCL→PCB→终端"完整链条。

---

## 6. 数据源说明

| 数据 | 来源 | 备注 |
|------|------|------|
| 实时行情 | 腾讯 qt.gtimg.cn | PE/PB/涨跌幅/市值 |
| 日K线 | 腾讯 ifzq.gtimg.cn | 前复权，免费 |
| A股列表 | 新浪 finance.sina.com.cn | 全量约5000只 |
| 资金流向 | akshare | 需要安装 akshare |
| 财务数据 | akshare（同花顺） | ROE/毛利率/现金流等 |
| LLM分析 | OpenAI API | 需配置 `OPENAI_API_KEY` |

---

## 7. 常见问题

**Q: 全市场排名跑到一半断了怎么办？**
```bash
python run/rank.py --resume   # 跳过已完成的股票，继续跑
```

**Q: 如何只分析自选池？**
将代码写入 `data/my_pool.csv`（每行一个6位代码），然后：
```bash
python core/quant_score.py --pool data/my_pool.csv
```

**Q: 回测结果太多，怎么看？**
`output/backtest/` 下按时间排序，最新的文件在最下面。`backtest_nav_*.csv` 是逐日净值，`backtest_trades_*.csv` 是每笔交易。

**Q: 趋势追涨回测用的是什么股票池？**
默认使用 `trend_strategy.py` 内置的AI算力产业链池。用 `--no-ai-pool --pool xxx.csv` 可以切换为自定义池。
