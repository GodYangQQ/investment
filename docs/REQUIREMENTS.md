# 需求台账 · 仓位管家

> 来源：`prompt/01-系统蓝图.md`、`prompt/02-交易方法论.md`。
> 规则：每条需求有编号、验收标准、证据。交付前逐条自测；未全绿不请求验收。
> 状态：✅ 已完成 · 🟡 部分 · ⬜ 待做

| 编号 | 需求 | 状态 | 验收标准 | 证据 |
|---|---|---|---|---|
| R-001 | 标的库：指数/个股/ETF 同表，含方向标签、池子、星标、分组、期权备注 | ✅ | 搜索添加后可在列表改标签/池子；指数与个股同表 | `core/position_store.py`、`run/position_manager.html` 标的页 |
| R-002 | 指数基准带：每个标的绑定主关联指数，指数方向标签参与入场闸门 | ✅ | 标的页可绑定主指数；看板顶部显示指数收盘/涨跌/标签/完成度 | `index_links`、`Service._idx_state` |
| R-003 | 价位必须经界面确认才入库；支持候选、确认、版本、失效替代 | ✅ | 未确认价位不参与评估；再次确认生成 v2 并 supersede v1 | `tests/test_position_store.py`、`active_levels()` |
| R-004 | 结构生命周期：待突破→进行中→已兑现→已衰竭；完成度纯数学 | ✅ | 收盘 > 颈线激活；峰值完成度 ≥90% 兑现；兑现后回撤 ≥15% 衰竭 | `tests/test_position_rules.py::TestStructure` |
| R-005 | 共振入场：个股站上颈线 + 主指数右侧；指数晚期半仓；否则只提示等待 | ✅ | 指数非右侧仅 `entry_wait`；指数完成度 ≥90% 时 `position=0.5` | `test_entry_gate_and_late` |
| R-006 | 离场规则：假突破清仓、头部颈线跌破、移动止盈、阶梯减仓、到目标终减 | ✅ | 收盘 < 颈线×0.95 清仓；峰值回撤 20% 清仓；70/80/90 阶梯只提示新跨过最高一档；到目标按指数终减 | `test_fake_break_and_trailing`、`test_ladder_and_final` |
| R-007 | 回撤买回 / 买点作废：进入买入区提示买回 N 成；跌破下沿×0.95 作废 | ✅ | 区间内 `pullback`；跌破 `pullback_invalid` | `test_sentinel_and_pullback` |
| R-008 | 方向标签压制；左侧→筑底唯一自动流转；筑底三判据只给候选 | ✅ | 左侧/筑底不产生入场/减仓；收盘进入起涨区自动转筑底；站上颈线/缩量放量/二次探底给候选 | `test_label_suppression`、`test_base_candidates` |
| R-009 | 哨兵：每个标的最多 2 条，距离分档，穿越提示 | ✅ | 第 3 条报错；距离 ≤3%/5%/15% 分级；穿越提示更新标签 | `add_sentinel()`、`test_sentinel_and_pullback` |
| R-010 | 信号去抖与优先级：P1 清仓 / P2 减仓 / P3 买卖 / P4 观察；同状态只提示一次 | ✅ | 重复评估同一天不重复入库；价位改版/换战役重新武装 | `record_signals()`、`tests/test_position_store.py` |
| R-011 | 看板：方向统计、指数带、操作提示、哨兵热度、持仓完成度 | ✅ | 首屏可见 6 个 KPI + 三张表；点标的进详情 | 截图 `看板`（本轮验收） |
| R-012 | 历史回放：按日线逐日跑规则，输出信号时间线 | 🟡 | 只回放价位生效日之后；模拟仓位随信号变化 | `Service.replay`；未做播放器 UI（仅弹窗列表） |
| R-013 | 日报：收盘后生成 Markdown；可推飞书 | 🟡 | 16:30 自动生成；配置 webhook 后可推送 | `build_report()`、`_scheduler`；待真实 webhook 验收 |
| R-014 | 模拟战役：入场自动记账、减仓/清仓、收益 | ✅ | 账户页显示入场价、剩余仓位、收益；不接交易接口 | `campaigns`、`campaign_pnl` 单测 |
| R-015 | 设置页：02 册参数全部可改，立即影响下一次计算 | ✅ | 参数落 SQLite；保存后评估使用新值 | `settings` 表、设置页 |
| R-016 | 禁硬删：核心表装触发器；删除一律软删/归档 | ✅ | 对核心表 DELETE 抛错；标的删除只写 `deleted_at` | `NO_DELETE_TABLES`、`test_no_hard_delete` |
| R-017 | 铁律③：规则引擎只吃日线收盘，不接触盘中/分钟数据 | ✅ | 规则模块输入仅 `daily_bars`；无分钟数据引用 | `core/position_rules.py` 无 I/O；`daily_bars` 表 |
| R-018 | 沙箱标的（TEST*）排除出列表/看板/统计/评估 | 🟡 | 列表默认过滤 `TEST%`；尚未提供合成 K 线夹具 | `list_instruments(exclude_test=True)` |
| R-019 | 图表画线：水平线/趋势线/纵向线；画完弹窗确认才入库 | 🟡 | 水平线/趋势线可确认入库；纵向线仅视觉参考；未做整线拖动/端点拖动 | `drawChart`、`finishDraw` |
| R-020 | 搜索个股/指数/ETF（名称/代码） | ✅ | 输入「沪电股份」或「002463」出建议；点击即添加 | `/api/search`（腾讯 smartbox） |
| R-021 | 多市场（A/HK/US） | ⬜ | `market` 字段已留；行情源目前仅 A 股 | — |
| R-022 | 截图/OCR 导入价位（只产生候选） | ⬜ | 识别结果进入候选区，必须人工确认 | — |
| R-023 | 备份与恢复（每日 dump + 演示恢复） | ⬜ | 备份文件可恢复；恢复演练记录 | — |
| R-024 | 画线样式记忆、全屏看板 | ⬜ | 刷新后样式保持；全屏模式画线可用 | — |
| R-025 | 持仓账户（成本/数量/融资/期权备注） | 🟡 | 期权备注字段已留；成本/数量未建表 | — |

## 本轮新增文件

- `core/position_rules.py` — 规则引擎（纯函数）
- `core/position_store.py` — SQLite 存储 / 版本 / 去抖 / 软删
- `core/price_data.py` — 日线抓取 + 搜索
- `run/position_manager.py` — HTTP API / 调度 / 日报 / 回放
- `run/position_manager.html` — 六页签 UI + K 线画线
- `tests/test_position_rules.py`、`tests/test_position_store.py`

## 未决问题

1. 「板块各选 30 个好的/差的」具体指向（左侧板块列表 30 涨 + 30 跌，还是资金流工具）待确认。
2. 飞书 webhook 需要用户提供后才能做真实推送验收。
3. 画线拖动/端点编辑是否纳入下一轮（蓝图 G-0 全功能矩阵）。
