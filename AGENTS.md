# AGENTS.md — 项目地图（给 AI Agent 的浓缩上下文）

> 目标：让下一个 Agent 先读这一页就能上手，不用全仓扫描，省 token。
> 详细说明见 `README.md`；本文件只保留「结构 + 数据流 + 命令 + 坑」。

## 1. 项目是什么

个人 **A股/美股投研 + 量化回测 + 看盘/画线/仓位管理** 的本地工具集。
Python 3.10+，Web 端是「单文件 Python 内置 HTTP 服务 + 配套 HTML」，无前端构建。

## 2. 目录结构

```
core/                      共享库（只放可复用逻辑）
├── datasets/              数据源：A股/美股/行情/池子
├── strategies/            策略：趋势/个股/A股炒作/板块热度/资金流/供需
├── filters/               市场/基本面过滤
├── events/                全球/世界事件
├── scoring/               量化打分
└── position/              仓位规则 + SQLite 存储

apps/
├── web/                   Web 应用
│   ├── sector_monitor_web.py/.html   板块划线看盘（主看盘）
│   ├── backtest_game.py              回测游戏（回放+模拟交易+画线）
│   ├── position_manager.py/.html     仓位管家
│   └── moneyflow_ui.py               板块资金流热力图
└── cli/                   命令行入口（选股/资金/事件/信号）
    rank / flow / events / cn_pick / cn_digest / cn_lhb / cn_moneyflow
    bottom_scan / soxl_strategy / us_smartmoney / repurchase / serenity_chain
    niusan_track / cn_alert

scripts/                   运维/预取（含 prefetch_sector_history.py）
data/                      运行时数据：DB/CSV/JSON（大多 gitignore）
tests/                     pytest
docs/ prompt/              文档/提示词
external/                  外部克隆仓库（gitignore，仅 README.md 入库）
chang_informs/             直播转写方法论 + 转写结果
```

## 3. 代码依赖图（简版）

```
apps/web/sector_monitor_web.py
  └─ core/datasets/* , data/sector_monitor.db
apps/web/backtest_game.py
  └─ core/datasets/{cn_data,us_data} , data/cmd , data/valuation
apps/web/position_manager.py
  └─ core/position/{position_rules,position_store} , data/position.db
apps/cli/*.py
  └─ core/{strategies,scoring,filters,events,datasets}
```

## 4. 数据流（重点）

### sector_monitor（实时看盘）
- 前端 `/api/bars` → `bars_payload()` → **SQLite `data/sector_monitor.db`**。
- **缓存优先**：日线 TTL 300s / 分钟 TTL 15s。
- 过期：先返回旧数据，后台**只增量拉最新**（日线 5 根、分钟 30 根），`INSERT OR REPLACE` 合并。
- 首次无缓存才全量抓（日线约 500–900 根）。
- 数据源：东财 → 同花顺 → 腾讯/新浪/Nasdaq（多源兜底）。
- 周/月线由日线重采样，不发请求。
- 画线：SQLite `lines` 表，key=`MARKET:code|tf`，schema `h:{y}` / `trend:{x1,y1,x2,y2}` / `v:{x,y1,y2}`（日期 ISO）。

### backtest_game（历史回放）
- `/api/kline` → `_bars_cn/_bars_us/_bars_index/_bars_cmd`。
- 数据：akshare（A股/ETF/指数）+ yfinance（美股）+ 本地商品/估值 JSON。
- 画线：`data/backtest_game_lines.json`（JSON 文件），schema 同上。

### 预取与更新
- 全量预取：`scripts/prefetch_sector_history.py --all`
- 增量更新：同上加 `--update`（只补最新 5 根）
- 写入 `data/sector_monitor.db` 的 `bars` 表。

## 5. 常用命令

```bash
# Web
python apps/web/sector_monitor_web.py --port 8771   # 看盘/画线 http://127.0.0.1:8771
python apps/web/backtest_game.py --port 8123        # 回测游戏
python apps/web/position_manager.py --port 8772     # 仓位管家
python apps/web/moneyflow_ui.py --port 8773         # 资金流热力图

# CLI（示例）
python apps/cli/rank.py
python apps/cli/flow.py
python apps/cli/events.py
python apps/cli/cn_pick.py

# 数据预取
python scripts/prefetch_sector_history.py --all --workers 4 --sleep 0.1
python scripts/prefetch_sector_history.py --all --update --workers 4

# 测试 / 语法
PYTHONPATH=. python -m pytest -q
python -m compileall -q core apps scripts tests
```

## 6. 约定与坑

- import 统一用 `core.<sub>.<module>`；入口脚本会自己把仓库根加入 `sys.path`。
- 新增数据源/策略放对应 `core/<sub>/`，不要塞进 `apps/`。
- 本机有代理 `http_proxy=127.0.0.1:7890`：
  - 本地 curl 用 `--noproxy '*'`；
  - YouTube 用 `yt-dlp -4`；
  - HF 下载慢/断可直连 + 断点续传。
- 数据接口可能限流/失败：必须有兜底（多源/缓存），不要假设一次成功。
- 不要提交：`data/*.db*`、`chang_informs/models|audio`、`external/` 里的实际仓库、`__pycache__`。
- `sector_monitor_web.py` 有 pandas 类型告警（静态检查噪音），`py_compile`/运行正常，不用去修。
- 回测口径（实验脚本）：次日开盘成交、手续费 0.05%、只做多、基准=买入持有。

## 7. 关键文件速查

| 要做的事 | 看这里 |
|---|---|
| A股/美股数据 | `core/datasets/{cn_data,us_data,price_data,universe}.py` |
| 量化打分 | `core/scoring/quant_score.py` |
| 仓位规则/存储 | `core/position/{position_rules,position_store}.py` |
| 看盘/画线 | `apps/web/sector_monitor_web.py` + `.html` |
| 回测游戏 | `apps/web/backtest_game.py` |
| 数据预取 | `scripts/prefetch_sector_history.py` |
| 直播转写 | `chang_informs/PROMPT.md` + `chang_informs/transcribe.py` |
| 外部仓库清单 | `external/README.md` |

## 8. 给 Agent 的硬规则

1. 先读本文件，再按需读目标文件；不要全仓遍历。
2. 改数据逻辑先看 `core/datasets/`；改看盘先看 `apps/web/sector_monitor_web.*`。
3. 不要修改 `external/` 里的第三方仓库；不要提交大模型/音频。
4. 需要真实数据时优先复用现有函数与 SQLite 缓存，不要另起一套。
5. 大改动前先跑 `pytest` 与 `compileall`；改完 Web 应用至少 curl 一次 200。
