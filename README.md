# Investment Lab

本地优先的 **A股 / 美股投研、量化评分、板块看盘、回测游戏与仓位管理** 工具集。

项目不是“荐股系统”，而是一套可复用、可修改、可离线运行的研究基础设施：

- 数据优先落本地 SQLite / JSON，接口失败时自动多源兜底。
- Web 端全部是「Python 标准库 HTTP 服务 + 原生 HTML」，没有 npm、没有前端构建。
- CLI 负责批量研究，Web 负责看盘、画线、回测和仓位纪律。
- 所有结论仅用于个人研究，不构成投资建议，也不接任何交易接口。

## 项目主次

| 优先级 | 模块 | 说明 |
|---:|---|---|
| 1 | **板块划线看盘** | 主应用。多周期 K 线、画线、自选、板块个股评分、离线缓存 |
| 2 | **回测游戏** | 历史回放、模拟交易、资金曲线、估值、仓位参考 |
| 3 | **本地工具** | 资金流热力图、仓位管家、CLI 批量研究、数据预取脚本 |

## 目录

- [快速开始](#快速开始)
- [板块划线看盘（主应用）](#板块划线看盘主应用)
- [回测游戏](#回测游戏)
- [本地工具](#本地工具)
- [架构总览](#架构总览)
- [目录结构](#目录结构)
- [核心库（core）](#核心库core)
- [数据与缓存](#数据与缓存)
- [数据源与兜底](#数据源与兜底)
- [量化评分](#量化评分)
- [配置项](#配置项)
- [开发与测试](#开发与测试)
- [开源与合规](#开源与合规)

## 快速开始

### 环境

- Python **3.10+**
- 建议 Linux / macOS；Windows 也可运行，路径相关脚本需自行调整
- 依赖见 `requirements.txt`

```bash
git clone https://github.com/GodYangQQ/investment.git
cd investment

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 主应用：板块看盘

```bash
# 可选：先预取板块 / 指数 / 美股 / 评分数据
python scripts/prefetch_all.py --all --workers 4 --sleep 0.1

# 启动看盘
python apps/web/sector_monitor_web.py --port 8771
# http://127.0.0.1:8771
```

不预取也能直接启动，在线模式会按需抓取并写入本地 SQLite。

### 回测游戏

```bash
python apps/web/backtest_game.py --port 8123
# http://127.0.0.1:8123
```

也可以从板块看盘顶部的「回测游戏」按钮进入。

### 本地工具

```bash
python apps/web/moneyflow_ui.py --port 8770       # 板块资金流热力图
python apps/web/position_manager.py --port 8781   # 仓位管家
python apps/cli/rank.py --top 10                  # CLI：SOXX 前20 排名
```

### 离线模式

```bash
# 先用在线模式或预取脚本把数据落库
python scripts/prefetch_all.py --all

# 之后只读本地库，不访问任何外部数据源
python apps/web/sector_monitor_web.py --offline --port 8771
# 或
SECTOR_OFFLINE=1 python apps/web/sector_monitor_web.py --port 8771
```

### 标的池与自选

美股默认研究宇宙是 **iShares Semiconductor ETF (SOXX) 前 20 大持仓**。
刷新标的池：

```bash
python core/datasets/universe.py
```

自选/持仓写在 `data/my_pool.csv`：

```text
代码,名称,层级,成本价,持仓量,买入日期
NVDA,NVIDIA,核心,120.00,50,2026-05-15
AMD,AMD,观察,,,
```

`SKHY`（海力士 ADR）若 Yahoo 无数据，会自动回退到 `000660.KS`。

## 板块划线看盘（主应用）

`apps/web/sector_monitor_web.py` + `apps/web/sector_monitor_web.html`

这是整个仓库最重要的部分：把「多周期 K 线 + 手动画线 + 自选看板」搬到浏览器，
标的从单只 ETF 扩展到 **A股板块 / 美股行业与主题 ETF**。

启动参数：

```bash
python apps/web/sector_monitor_web.py                 # 默认 127.0.0.1:8771
python apps/web/sector_monitor_web.py --port 8888
python apps/web/sector_monitor_web.py --host 0.0.0.0  # 局域网访问
python apps/web/sector_monitor_web.py --no-warm       # 不预热、不后台刷新
python apps/web/sector_monitor_web.py --offline       # 只读本地库
```

### 看盘能力

- A股：东方财富行业 / 概念板块；东方财富失败时自动切同花顺行业表。
- 美股：SPDR 11 大行业 + 半导体/科技（SOXX/SMH/IGV…）+ 主题 ETF（XBI/KRE/XOP/GLD…）。
- 周期：分时 / 5 / 15 / 30 / 60 分 / 日 / 周 / 月。周/月线由日线本地重采样。
- K 线：收盘折线、成交量、MA5/17/21/45 单独开关、对数坐标、十字光标、OHLC 浮窗、
  滚轮缩放、拖拽平移。
- 排序：成交额 / 涨跌幅 / 量比 / 主力净额；支持搜索板块 / 个股 / 指数。
- 板块明细：标题栏「板块明细」打开**全量板块表**（不受左侧“前15/中15/后15”截断），
  点表头可按 成交额 / 主力净额 / 涨跌幅 / 量比 / 板块名 升降序切换；支持名称过滤，
  点任意行跳到该板块 K 线；空数据列（如数据源没有量比）自动隐藏。直达链接 `/#detail`。
  - **共振 / 对手**：每行给出 2 个绿 tag（共振，同涨同跌）和 2 个红 tag（对手，剔掉大盘后反向），
    点 tag 直接跳到那个板块。
  - 口径：近 250 个交易日日收益。共振用原始相关；对手用「板块收益 − 上证指数收益」的
    超额收益相关（原始相关几乎全正，只有剔掉大盘 beta 才会出现真正的反向轮动）；
    同一个板块的两套代码（东财 `BK…` / 同花顺 `T88…` 相关≈1）自动去重。计算在
    `core/scoring/sector_relation.py`，结果内存缓存 30 分钟。
- 画线：水平线 / 趋势线 / 纵向线；拖线体平移、拖端点调整、Delete 删除、Ctrl+Z/Y 撤销重做。
- 自选：自选板块卡片网格，支持滚轮滚动、单击选中、双击放大/画线；
  鼠标悬停时弹出一个 **3 倍尺寸的预览图**（内容按 3 倍重新排版、K 线按 3 倍像素重绘，所以是清晰的；
  卡片本体大小不变，**hover 命中区仍是原来那一小块**，预览层不接管鼠标，不挡相邻卡片）。
- 自选卡片标签：每张卡片左上角标 **A股 / 美股**；ETF 卡片 hover 出现「持仓」按钮，
  打开前 10 大成分股（A股走基金持仓季度数据，美股走 SPDR 官方 xlsx / iShares 快照 / yfinance），
  点成分行可直接打开该股 K 线。
- 板块评分：自选板块卡片 hover 出现「个股评分」按钮，打开独立窗口：
  - 最多展示评分前 **50** 只；
  - 每只股票显示日线折线简图、评分、成长、盈利、质量、回购、ROE；
  - 科创板 / 创业板 / 上证 / 深证 / 北交所等标签可点击筛选；
  - 点击某行直接打开该股 K 线。

### 数据策略

- 缓存优先 + SWR：有缓存先返回，过期后后台只补最新几根。
- 日线 TTL 约 300 秒，分钟线约 15 秒。
- 首次无缓存才全量抓取；之后日线补 5 根、分钟线补 30 根。
- 同一 key 同时只有一个抓取任务，避免重复打接口。
- 接口失败时回落到本地数据库最近一次数据，并在界面标注「缓存 / 同步中」。
- `--offline` 只读本地 SQLite，不访问任何外部接口。

### 主要 HTTP API

| 路径 | 说明 |
|---|---|
| `/api/sectors` | 板块/ETF 列表与实时快照 |
| `/api/bars` | K 线（板块/个股/指数/ETF） |
| `/api/board_scores` | 板块个股评分，支持 `top` 参数 |
| `/api/sector_relations` | 板块共振 / 对手榜（近 N 日收益相关） |
| `/api/etf_holdings` | ETF 前 N 大持仓（美股 SPDR/SSGA、A股基金持仓） |
| `/api/watch` | 自选列表读写 |
| `/api/lines` | 画线读写 |
| `/api/sectors_ma` | MA21 斜率筛选 |
| `/api/stats` / `/api/diag` | 本地库状态 / 数据源连通性 |

### 本地存储

`data/sector_monitor.db`（SQLite / WAL，**不入库**，行情可重抓）

| 表 | 内容 |
|---|---|
| `bars` | K 线，按 `market + code + tf` 存储 |
| `meta` | 每份 K 线的抓取时间与来源 |
| `snapshots` | 板块列表 / 行情快照 |
| `cache` | 其他通用缓存 |

**画线和自选存成 JSON（可以提交到 git）**：

| 文件 | 内容 | 体量 |
|---|---|---|
| `data/sector_lines.json` | 画线，key = `MARKET:code\|tf` | 约 8 KB |
| `data/watch.json` | 自选列表 | 约 1 KB |

两者是手工产出的小数据，`.gitignore` 已放行，可随仓库备份/同步；
老版本存在 SQLite 里的 `lines` / `watch` 会在首次启动时自动迁移成 JSON。

画线 schema：

```text
h: {y}
trend: {x1, y1, x2, y2}
v: {x, y1, y2}
```

> 旧版 `data/sector_lines.json` 会在首次启动时自动迁移进 SQLite。

### 看盘数据源

| 场景 | 主源 | 兜底 |
|---|---|---|
| A股板块列表 | 东方财富 | 同花顺 → 本地缓存 |
| A股板块 K 线 | 东方财富 | 同花顺 → 本地 SQLite |
| A股个股行情 | 腾讯 | 新浪 |
| 美股行情 / K 线 | 新浪 | Nasdaq → 本地 SQLite |
| 美股列表 / 实时 | 新浪批量行情 | 本地缓存 |

## 回测游戏

`apps/web/backtest_game.py`

- 市场：A股 / 美股 / 指数 / 商品（原油、黄金、白银、铜、天然气等）。
- 流程：选板块 → 选个股/ETF → 选开始日期 → 逐步回放。
- 每个交易日两个时点：09:55 用开盘价、14:55 用收盘价；支持上一步 / 下一步。
- 模拟买卖，双边手续费 0.05%，对比「买入持有」基准。
- 统计胜率、区间收益、年化波动、夏普、收益/回撤。
- 估值面板：PE/PB/PS + 近 5 年历史分位（本地缓存 6 小时）。
- 仓位参考：波动率目标建议仓位 = `min(1, 目标波动 ÷ 年化波动)`，不自动交易。
- 画线与看盘使用同一套 schema，保存在 `data/backtest_game_lines.json`。

启动：

```bash
python apps/web/backtest_game.py --port 8123
```

## 本地工具

### 资金流热力图

`apps/web/moneyflow_ui.py`，默认端口 8770。

- 板块资金流方框热力图：越流入越红，越流出越绿。
- 资金来源为按成交金额分档的近似口径：

| 展示口径 | 近似来源 |
|---|---|
| 机构 / 主力 | 超大单 + 大单 |
| 机构（纯） | 超大单 |
| 游资（近似） | 大单 |
| 散户 | 中单 + 小单 |

> 这是估算口径，不是账户级真实资金流。真实席位需结合龙虎榜。

### 仓位管家

`apps/web/position_manager.py` + `apps/web/position_manager.html`，默认端口 8781。

- 原则：**判断归人、计算归机器、纪律归系统**。
- 方向标签由人设定并压制自动判断；价位必须点「确认入库」才生效。
- 规则只读日线收盘价；质地分不参与信号；不接交易接口。
- 功能：看板、标的库、K 线画线、突破/头部确立、回撤区、哨兵、信号、账户、设置。
- 规则：结构生命周期、共振入场、70/80/90 阶梯减仓、目标终减、移动止盈、假突破清仓等。
- 存储：`data/position.db`（SQLite / WAL），核心表禁止硬删，标的删除只写 `deleted_at`。
- 日报：工作日 16:30 自动评估并生成 Markdown；配置飞书 webhook 后可推送。

### 命令行工具（apps/cli）

所有 CLI 都从仓库根目录运行，入口脚本会自动把仓库根加入 `sys.path`。

#### 美股 / SOXX

| 命令 | 用途 |
|---|---|
| `python apps/cli/rank.py --top 10` | SOXX 前20 多因子量化排名 |
| `python apps/cli/flow.py --days 5` | 美股资金流（成交额 × 涨跌方向代理） |
| `python apps/cli/soxl_strategy.py` | SOXL 双均线趋势策略 + 回测 |
| `python apps/cli/us_smartmoney.py insider NVDA` | SEC 内部人 Form 4 |
| `python apps/cli/us_smartmoney.py fund --name "Berkshire"` | 机构 13F 持仓 |
| `python apps/cli/serenity_chain.py` | 供应链卡点强度排序 |
| `python apps/cli/events.py --top 20` | 全球事件 → 美股候选标的 |

#### A股

| 命令 | 用途 |
|---|---|
| `python apps/cli/cn_pick.py` | 全球事件 → 题材模块 → 模块前3龙头 |
| `python apps/cli/cn_alert.py` | 活跃/异动个股预警 |
| `python apps/cli/cn_digest.py` | 每日资金摘要（大盘/板块/个股/龙虎榜） |
| `python apps/cli/cn_moneyflow.py` | 板块与个股资金流排名 |
| `python apps/cli/cn_lhb.py` | 龙虎榜明细与机构席位 |
| `python apps/cli/bottom_scan.py --board BK1329` | 板块成分股抄底/吸筹扫描 |
| `python apps/cli/repurchase.py` | 回购 / 股东增持查询 |
| `python apps/cli/niusan_track.py --stock 600812` | 十大流通股东 / 牛散追踪 |

常用示例：

```bash
# 美股全池排名
python apps/cli/rank.py --top 10

# A股短线链路
python apps/cli/cn_pick.py --top-modules 12 --top-stocks 3

# 只看某类事件
python apps/cli/events.py --category war_geopolitics --top 30
python apps/cli/cn_pick.py --category plague

# 资金流
python apps/cli/flow.py --days 5 --top 20
python apps/cli/cn_digest.py --top 10
```

### 数据更新与运维脚本（scripts）

这一节集中说明所有 `scripts/` 入口。

#### 数据预取 / 更新

| 脚本 | 作用 | 常用命令 |
|---|---|---|
| `prefetch_all.py` | 一次性预取板块、指数、美股、评分 | `python scripts/prefetch_all.py --all --workers 4 --sleep 0.1` |
| `prefetch_sector_history.py` | 只预取板块/指数/美股 K 线到 SQLite | `python scripts/prefetch_sector_history.py --all --workers 4` |
| `backfill_cn_stocks.py` | 慢速、可续跑补齐 A 股 15 年日线 | `python scripts/backfill_cn_stocks.py --wait 1200 --sleep 2` |
| `regenerate_csv.py` | SOXX 池最新行情 → `output/strategy_YYYYMMDD.csv` | `python scripts/regenerate_csv.py` |
| `update_holdings_data.py` | 持仓评分/指标 → `dashboards/holdings_data.json` | `python scripts/update_holdings_data.py` |

日常增量更新：

```bash
# 只补最新 5 根日线，最快
python scripts/prefetch_sector_history.py --all --update --workers 4
```

首次全量预取：

```bash
# 板块 + 指数 + 美股 + 评分
python scripts/prefetch_all.py --all --workers 4 --sleep 0.1

# 加上项目股票池 / 自选 / 美股热门
python scripts/prefetch_all.py --all --with-stocks --workers 4 --sleep 0.2

# 只构建评分缓存
python scripts/prefetch_all.py --scores
```

`prefetch_sector_history.py` 的主要参数：

| 参数 | 说明 |
|---|---|
| `--all` | A股行业 + 概念 + 指数 + 美股 |
| `--stocks` | 加上项目股票池/自选/美股热门 |
| `--cn-all` | 加上全部 A 股（约 5000 只，很重） |
| `--update` | 只补最新 5 根日线 |
| `--limit N` | 只处理前 N 个，调试用 |
| `--workers N` | 并发数 |
| `--sleep S` | 每个任务后休眠秒数，防限流 |

#### 监控 / 辅助

| 脚本 | 作用 | 常用命令 |
|---|---|---|
| `monitor_ambush.py` | 每 5 秒拉实时价，按距最近买点排序 | `python scripts/monitor_ambush.py` |
| `collect_risk_data.py` | 采集风险看板指标 → `output/risk_data_YYYYMMDD.json` | `python scripts/collect_risk_data.py` |
| `fetch_x_user.py` | 抓取 X/Twitter 用户最近推文 | `python scripts/fetch_x_user.py aleabitoreddit --limit 20` |
| `okx_probe.py` | 检查 OKX 是否上架某标的 | `python scripts/okx_probe.py SOXX` |
| `rsrs_slope.py` | RSRS 斜率/分查看 | `python scripts/rsrs_slope.py --indices` |
| `top_performers_5y.py` | SOXX 前20 近 5 年涨幅倍数排名 | `python scripts/top_performers_5y.py` |

`fetch_x_user.py` 可选使用官方 API：

```bash
X_BEARER_TOKEN=xxxx python scripts/fetch_x_user.py aleabitoreddit --limit 20
```

> `output/`、数据库、画线、交易记录等运行时文件默认不入库，见 `.gitignore`。

## 架构总览

```text
外部数据源
  东财 / 同花顺 / 腾讯 / 新浪 / Nasdaq / yfinance / iShares
  Google News / GDELT / USGS / WHO / CDC / SEC EDGAR
        │
        ▼
core/datasets        数据获取、字段标准化、多源兜底
core/events          全球事件采集与逻辑链映射
core/strategies      趋势、资金流、板块热度、供需、A股炒作
core/scoring         多因子量化评分、A股业绩+回购评分
core/filters         基本面 / 市场过滤
core/position        仓位规则引擎 + SQLite 存储
        │
        ├── apps/cli      批量研究、报告、排名、事件扫描
        │
        └── apps/web      HTTP 服务 + 原生 HTML
              ├── sector_monitor_web   主应用：看盘 / 画线 / 自选 / 评分榜
              ├── backtest_game        历史回放 / 模拟交易 / 估值
              ├── moneyflow_ui         板块资金流热力图
              └── position_manager     仓位纪律 / 信号 / 日报
        │
        ▼
data/
  SQLite：sector_monitor.db / position.db
  JSON / CSV：自选、逻辑链、估值、画线、交易记录
```

### 四条主要数据流

1. **Web 看盘流（主链路）**
   浏览器 → `/api/sectors`、`/api/bars`、`/api/board_scores` → `apps/web/sector_monitor_web.py`
   → `data/sector_monitor.db`。缓存优先，过期后先返回旧数据，再后台增量刷新。

2. **回测流**
   浏览器 → `/api/kline`、`/api/trade` → `apps/web/backtest_game.py`
   → akshare / yfinance / 本地商品与估值数据 → 画线与交易记录 JSON。

3. **批量研究流**
   `apps/cli/*` → `core/strategies/*` / `core/scoring/*` → 终端输出或 `output/*.csv`。

4. **预取与仓位流**
   `scripts/prefetch_*.py` → 数据源 → `sector_monitor.db`；
   人工画线并确认价位 → `position.db` → 规则引擎 → 信号 / 日报。

## 目录结构

```text
core/                       共享库（可复用逻辑）
├── datasets/               A股/美股/行情/标的池
├── events/                 全球事件、世界事件逻辑链
├── strategies/             趋势、资金流、板块热度、供需、A股炒作
├── scoring/                量化评分、公司业绩+回购评分
├── filters/                市场/基本面过滤
└── position/               仓位规则 + SQLite 存储

apps/
├── web/
│   ├── sector_monitor_web.py/.html   板块划线看盘（主应用）
│   ├── backtest_game.py              回测游戏
│   ├── position_manager.py/.html     仓位管家
│   └── moneyflow_ui.py               板块资金流热力图
└── cli/                    命令行入口（选股/资金/事件/信号）

scripts/                    数据预取、回填、监控、研究脚本
data/                       运行时数据与知识库（大部分运行时文件不入库）
docs/                       需求、模板、风控文档
prompt/                     交易方法论与 AI 协作提示词
tests/                      pytest 测试
external/                   外部参考仓库（仅 README 入库）
chang_informs/              直播转写方法论与转写结果
```

## 核心库（core）

| 路径 | 说明 |
|---|---|
| `core/datasets/cn_data.py` | A股概念板块、成分、腾讯行情/K线 |
| `core/datasets/us_data.py` | 美股 Nasdaq / yfinance 数据层 |
| `core/datasets/price_data.py` | 个股/指数/ETF 日线，腾讯前复权 + 新浪兜底 |
| `core/datasets/universe.py` | SOXX 前20 标的池 |
| `core/events/global_events.py` | GDELT / WHO / CDC / USGS / Bluesky 等权威事件 |
| `core/events/world_events.py` | 世界事件 → 逻辑链 → 美股标的 |
| `core/strategies/cn_speculation.py` | A股事件 → 题材模块 → 龙头 |
| `core/strategies/money_flow.py` | 美股资金流代理 |
| `core/strategies/sector_heat.py` | 半导体细分赛道景气度 |
| `core/strategies/supply_demand.py` | 供需六指标评估 |
| `core/strategies/trend_strategy.py` | 趋势追涨策略 |
| `core/scoring/quant_score.py` | 4 因子量化评分 + 仓位建议 |
| `core/scoring/company_score.py` | A股业绩 + 回购行业评分 |
| `core/filters/*` | 基本面与市场过滤 |
| `core/position/position_rules.py` | 仓位规则纯函数（可单测） |
| `core/position/position_store.py` | 仓位 SQLite 存储 |

## 数据与缓存

### 入库的知识库 / 配置

| 文件 | 用途 |
|---|---|
| `data/my_pool.csv` | 自选/持仓池 |
| `data/event_chains.json` | 美股事件逻辑链 |
| `data/cn_event_chains.json` | A股题材炒作逻辑链 |
| `data/serenity_chain.json` | 供应链卡点映射 |
| `data/soxx_holdings.csv` | SOXX 持仓快照 |
| `data/cmd/*.json` | 商品历史数据 |
| `data/valuation/*.json` | 估值缓存快照 |

### 运行时文件（默认 gitignore）

| 文件 | 用途 |
|---|---|
| `data/sector_monitor.db` | 看盘 K 线、画线、自选、快照 |
| `data/position.db` | 仓位管家数据 |
| `data/backtest_game_lines.json` | 回测游戏画线 |
| `data/backtest_game_trades.json` | 回测游戏交易记录 |
| `data/ui_cache.json` | 资金流 UI 缓存 |
| `data/company_scores.json` | A股公司评分缓存 |
| `output/` | CLI / 脚本输出 |

## 数据源与兜底

| 场景 | 主源 | 兜底 |
|---|---|---|
| A股板块列表 | 东方财富 | 同花顺 → 本地缓存 |
| A股板块 K 线 | 东方财富 | 同花顺 → 本地 SQLite |
| A股个股行情 | 腾讯 | 新浪 |
| 美股行情 / K 线 | Nasdaq | 新浪 → yfinance |
| SOXX 持仓 | iShares 官网 CSV | 内置快照 |
| 全球事件 | GDELT / 官方 RSS / USGS | Google News 权威域名 |
| 内部人 / 13F | SEC EDGAR | — |
| A股资金流 | 东方财富 / 同花顺（akshare） | 本地缓存 |

设计原则：

- 不假设任何单一接口永远可用。
- 能缓存就缓存，能增量就不全量。
- 接口失败时优先返回旧数据，并在界面标注「缓存 / 同步中」。
- 外部接口可能限流，遇到异常先降并发、加 `--sleep`。

## 量化评分

### 美股 4 因子（满分 100）

| 因子 | 权重 | 内容 |
|---|---:|---|
| 趋势 + 位置 | 35 | MA20/MA60、价格在 20/60 日区间位置 |
| 量价 + Price Action | 30 | 量比、实体、影线、缺口 |
| 波动率 | 15 | ATR、近端波动 |
| 斐波那契 / 结构 | 20 | 回撤位置、局部高低点结构 |

可买度：`>=78` 强烈推荐 / `68-77` 推荐 / `55-67` 关注 / `40-54` 偏弱 / `<40` 回避。

### A股公司评分

`core/scoring/company_score.py` 按行业内百分位综合：

- 最新业绩：营收/净利同比、ROE、毛利率等；
- 回购：已回购金额、计划上限、执行率、回购/净利；
- 输出 0-100 分，供看盘「板块个股评分」窗口使用。

> 评分是研究和排序工具，不替代基本面判断，也不构成买卖建议。

## 配置项

| 环境变量 | 说明 |
|---|---|
| `SECTOR_OFFLINE=1` | 板块看盘只读本地库，等价于 `--offline` |
| `POSITION_DB=/path.db` | 覆盖仓位管家数据库路径 |
| `POSITION_FEISHU_WEBHOOK=...` | 仓位日报飞书 webhook |
| `X_BEARER_TOKEN=...` | `fetch_x_user.py` 使用官方 X API |

本地代理注意事项：

- 本地 `curl` 调试可用 `--noproxy '*'`。
- 数据源接口对代理敏感，失败时先检查网络与限流。

## 开发与测试

```bash
# 测试
PYTHONPATH=. python -m pytest -q

# 语法检查
python -m compileall -q core apps scripts tests

# 可选：代码风格（需自行安装 ruff / black）
ruff check .
black --check .
```

项目约定：

- 新增数据源/策略放 `core/<子模块>/`，不要塞进 `apps/`。
- Web 入口只做 HTTP、页面和编排，可复用逻辑放 `core/`。
- 数据接口必须有兜底（多源 / 缓存），不要假设一次成功。
- 不提交 `data/*.db*`、个人自选、交易记录、`output/`、`external/` 中的第三方仓库。
- 详见 `AGENTS.md`（给 AI Agent 的项目地图）。

## 开源与合规

- 本项目仅用于个人学习、研究与技术交流，**不构成投资建议**。
- 行情、财务、新闻数据来自公开接口，使用时请遵守各数据源的服务条款与频率限制。
- 不要把账号、Cookie、Token、webhook 或个人持仓数据提交到仓库。
- 短线炒作、杠杆 ETF、事件驱动策略风险极高；任何实盘决策请自行独立判断。
- 当前仓库尚未附带 `LICENSE` 文件。正式开源前建议选择并补充许可证（如 MIT / Apache-2.0），并确认第三方数据与代码的授权范围。
