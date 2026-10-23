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
| 板块划线看盘（A股/美股 Web） | `python run/sector_monitor_web.py` |

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

## 板块划线看盘（Web · A股 / 美股）

`run/sector_monitor_web.py` 是 `run/soxx_monitor.py`（PyQt5）的 **Web 版**：画线交互完全照搬 Qt 版，标的从单只 ETF 换成**板块**，浏览器里就能用。

```bash
python run/sector_monitor_web.py              # http://127.0.0.1:8771
python run/sector_monitor_web.py --port 8888
python run/sector_monitor_web.py --host 0.0.0.0   # 局域网访问
python run/sector_monitor_web.py --no-warm        # 不预热、不后台刷新（只读缓存）
```

- **标的池**
  - A股：东方财富行业 / 概念板块，可按 **成交额 / 涨跌幅 / 量比 / 主力净额** 排序，搜索框过滤；
  - 美股：SPDR 11 大行业 + 半导体·科技（SOXX/SMH/IGV…）+ 主题（XBI/KRE/XOP/GLD…）；
    想增删 ETF 改 `run/sector_monitor_web.py` 顶部的 `US_GROUPS` 即可。
- **文件**：后端 `run/sector_monitor_web.py`，前端 `run/sector_monitor_web.html`（需与 .py 同目录，由服务端读取）。
- **图表**：分时 / 5 / 15 / 30 / 60 分 / 日 / 周 / 月；收盘折线加粗高亮 + 成交量，MA5/17/21/45 **每条可单独开关**（默认全关，只看收盘折线）；
  **纵轴默认对数坐标**（`对数` 按钮可切回普通价格坐标，选择存 `localStorage.sm_log`），长期看涨幅不失真；十字光标、OHLC 浮窗、滚轮缩放、拖拽平移。
- **自选：全屏看板**
  - 左侧列表点 ☆ 收藏，右侧「自选」按钮打开**全屏看板**；所有自选排成一行卡片，**不需要翻页**，
    按住空白处拖动即可横向滚动（滚轮也能横向滚）；
  - 每张卡片可拖右下角**单独改宽高**（存 `localStorage.sm_watch`，下次打开保持），显示迷你收盘折线、
    已画的线、最新价，以及**最近的支撑（绿）和压力（红）**；
  - 单击卡片选中，**双击 = 放大**（打开该板块并进入单图专注模式，`Esc` 退出），也可点卡片上的「放大」；
  - 看板开着时卡片每 60 秒自动刷新一次；顶部标题栏也会实时显示当前板块的支撑 / 压力。
- **画线（与 Qt 版一致）**：
  - 水平线 / 趋势线（两点成线）/ **纵向线**（同一时刻上下两价，直接标出这段涨幅 %），**画完一根自动退出画线模式**，按钮复位；
  - 画线模式开着时点到已有线，会**优先选中那条线并自动退出画线模式**，不会再多画一条（避免串线）；
  - 点线选中 → 拖线体平移、拖两端圆点调整；`Delete` 删除；`Ctrl+Z / Ctrl+Y` 撤销重做；右键 / `Esc` 退出；
  - 颜色：现价在线上方 = 支撑（绿），下方 = 压力（红），随行情实时重算；
  - 按 `市场:板块代码|周期` 存 `data/sector_lines.json`，schema 与 Qt 版相同（`{type:"h",y}` / `{type:"trend",x1,y1,x2,y2}` / `{type:"v",x,y1,y2}`）。
- **数据源与兜底**

| 市场 | 列表/实时 | 分时 | 日/周/月 |
|---|---|---|---|
| A股 | 东财 `clist`（失败→同花顺行业表→本地缓存） | 东财 `trends2`/`kline` → 同花顺板块分时 | 东财 `kline` → 同花顺指数 → 东财资金流收盘价 |
| 美股 | 新浪 `gb_` 批量实时 | 新浪 `MinK` | 新浪 `DailyK` → Nasdaq |

指数条（上证/深证/创业板）东财不通时自动切新浪。所有接口失败时回落到本地数据库的最近一次数据，并在右上角标注「缓存」。

> 同花顺兜底板块（代码以 `T` 开头）同样支持分时 / 5 / 15 / 30 / 60 分钟、日/周/月；
> 分时用当日 1 分钟实时合成，历史不足时自动回退到 `last.js` 的最近 140 根。

- **本地数据库（秒开 + 断网可看）**
  - `data/sector_monitor.db`（SQLite / WAL）：`bars` 存K线（只追加、反复读）、`meta` 存每份K线的抓取时间与来源、`snapshots` 存板块列表/行情快照；分时只保留最近 25 天。
  - **SWR（先给旧数据，再后台刷新）**：有数据立刻返回，过期才在后台补拉；同一 key 同时只有一个请求。启动时预热列表 + 成交额前 12 个 A 股板块 / 前 8 个美股 ETF 的日线，已有新鲜数据则跳过，重启不重复打接口。
  - **周/月线由日线本地重采样**，不再单独请求，切换周期毫秒级。
  - 前端还有一层：内存 LRU 缓存 36 条、`AbortController` 取消旧请求、选中板块后预取日/周/月、刷新页面从 `sessionStorage` 恢复上次视图（含画线位置）。
  - HTTP 走 `HTTP/1.1` keep-alive + gzip（>900B 的 JSON）。

## 仓位管家（Web · 规则引擎 + 看板）

`run/position_manager.py` 按 `prompt/` 的蓝图实现「判断归人、计算归机器、纪律归系统」：
你在图上画颈线 / 量度目标并确认，系统每天收盘后只用**日线收盘价**做数学比较，到点提醒。

```bash
python run/position_manager.py                 # http://127.0.0.1:8781
python run/position_manager.py --port 8782
POSITION_DB=/tmp/x.db python run/position_manager.py   # 换库（测试用）
python -m unittest tests.test_position_rules tests.test_position_store -v
```

- **铁律**：方向标签由人设定并压制自动判断；价位必须点「确认入库」才生效；规则只读 `daily_bars`；
  质地分不参与信号；不接任何交易接口。
- **页面**：看板（方向统计 / 指数基准 / 操作提示 / 哨兵热度 / 持仓完成度）、标的库（搜索个股/指数/ETF，
  改方向标签与池子）、详情（K 线 + 画线 + 突破确立 + 头部确立 + 回撤区 + 哨兵 + 信号）、信号、账户、设置。
- **规则**（`core/position_rules.py`，纯函数，17 个单测）：
  结构生命周期（待突破→进行中→已兑现→已衰竭）、共振入场（指数右侧 + 指数晚期半仓）、
  70/80/90 阶梯减仓（只提示新跨过的最高一档）、到目标按指数终减、-20% 移动止盈、
  颈线×0.95 假突破清仓、头部颈线跌破、回撤买回/作废、左侧→筑底自动流转、筑底三判据、哨兵分档。
- **画线**：水平线 / 趋势线 / 纵向线；画完弹窗选类型（颈线/目标/头部/前高/回撤区）→「确认入库」，
  以 `source=drawing` 写入价位表；改版自动生成 v2 并 supersede 旧版。
- **存储**：`data/position.db`（SQLite / WAL），核心表装触发器**禁止硬删**；标的删除只写 `deleted_at`。
  信号按 `(标的, 规则, state_key)` 永久去抖，价位改版 / 换战役会重新武装。
- **日报**：工作日 16:30 自动评估并生成 Markdown；配置 `feishu_webhook`（或 `POSITION_FEISHU_WEBHOOK`）
  后 `POST /api/push` 推送。历史回放：`GET /api/replay?code=SZ002463&days=250`。
- **需求台账 / 回归清单**：`docs/REQUIREMENTS.md`、`docs/REGRESSION.md`。

## 目录

```
core/     美股数据/打分/供需/资金流/细分 + 全球事件/A股炒作
run/      rank / flow / screen / backtest / events / cn_pick / soxx_monitor / sector_monitor_web / position_manager
ml/       特征矩阵
data/     my_pool.csv、event_chains.json、cn_event_chains.json
docs/     分析模板、仓位风控
scripts/  持仓看板、RSRS、监控
```

## 分析模板

深度个股分析走 `docs/PROMPT_TEMPLATE.md`：消息面四层（公司 / 半导体行业 / 宏观Fed / SOXX 市场）、供需三层、供应链卡点、形态、止盈止损。
