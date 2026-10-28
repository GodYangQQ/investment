#!/usr/bin/env python3
"""
回测游戏（看盘 + 模拟交易 + 画线）
==================================
一个自包含的网页小游戏：
  · 选板块 → 选个股（板块/标的支持中文搜索，也可直接输代码 / 美股 ticker）→ 选开始日期
  · 市场：A股 / 美股 / 指数 / 商品
     商品含布伦特原油、WTI、伦敦金/银、COMEX 金/银/铜、天然气（新浪全球期货日线）
  · 每天两个交易时点（09:55 用开盘价、14:55 用收盘价）；「下一步 / 上一步」可逐步回放
      （←/→ 方向键；上一步会撤销该步之后的状态与交易）
  · 可买入 / 卖出（双边 0.05% 手续费），实时显示你的资金曲线 vs「买入持有」基准
  · 每笔「建仓 → 清仓」统计胜率
  · 左侧「至今表现」：自开始回放起算的区间收益、年化波动、夏普、收益/回撤，与持有对比
      （至少 30 个交易日才显示，避免短样本年化失真）
  · 左侧「估值」面板：当日 PE/PB/PS + 近 5 年历史分位，绿<30% / 黄30-70% / 红>70%
      （数据来自东财 datacenter，本地缓存 data/valuation/<code>.json，6 小时有效）
  · 左侧「仓位参考」面板：波动率目标建议仓位 = min(1, 目标波动 ÷ 年化波动)
      目标波动 15/20/25/30%、回看 60/80/120 日可调，并显示与当前仓位的偏离；不自动交易
  · 画线（水平线 / 趋势线 / 纵向线）逻辑对齐 apps/web/sector_monitor_web.html：
      点第一个点 → 移动实时预览 → 点第二个点完成；左键点线选中，拖线体平移、
      拖两端圆点调整；Delete 删除、Ctrl+Z/Y 撤销重做、右键/Esc 退出画线
  · 画线按 市场:标的|周期 保存到 data/backtest_game_lines.json
      （与 sector_monitor 同一 schema：h: y / trend: x1,y1,x2,y2 / v: x,y1,y2）

启动：
    python apps/web/backtest_game.py [--port 8123]
然后浏览器打开 http://127.0.0.1:8123
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import uuid
from bisect import bisect_left, bisect_right, insort
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.datasets.cn_data import get_concept_boards, get_board_constituents, get_kline  # noqa: E402
from core.datasets.us_data import fetch_daily_kline  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LINES_FILE = os.path.join(DATA_DIR, "backtest_game_lines.json")
TRADES_FILE = os.path.join(DATA_DIR, "backtest_game_trades.json")
CMD_DIR = os.path.join(DATA_DIR, "cmd")

# 商品（新浪全球期货日线）。code 同时作为接口代码，symbol 为新浪代码。
COMMODITIES = [
    {"code": "OIL", "symbol": "OIL", "name": "布伦特原油", "unit": "美元/桶",
     "alias": ["布伦特", "布油", "原油", "brent", "oil"]},
    {"code": "CL", "symbol": "CL", "name": "WTI原油", "unit": "美元/桶",
     "alias": ["wti", "美油", "原油", "cl"]},
    {"code": "XAU", "symbol": "XAU", "name": "伦敦金", "unit": "美元/盎司",
     "alias": ["黄金", "伦敦金", "gold", "xau"]},
    {"code": "XAG", "symbol": "XAG", "name": "伦敦银", "unit": "美元/盎司",
     "alias": ["白银", "伦敦银", "silver", "xag"]},
    {"code": "GC", "symbol": "GC", "name": "COMEX黄金", "unit": "美元/盎司",
     "alias": ["黄金", "comex", "gold", "gc"]},
    {"code": "SI", "symbol": "SI", "name": "COMEX白银", "unit": "美元/盎司",
     "alias": ["白银", "comex", "silver", "si"]},
    {"code": "HG", "symbol": "HG", "name": "COMEX铜", "unit": "美元/磅",
     "alias": ["铜", "copper", "hg"]},
    {"code": "NG", "symbol": "NG", "name": "天然气", "unit": "美元/百万英热",
     "alias": ["天然气", "gas", "ng"]},
]
_CMD_BY_CODE = {c["code"]: c for c in COMMODITIES}

# 东财板块接口不稳定时的兜底板块（保证「选板块」永远可用）
FALLBACK_BOARDS = [
    {"code": "fb_semicon", "name": "半导体", "stocks": [
        ("002371", "北方华创"), ("603501", "韦尔股份"), ("002049", "紫光国微"),
        ("688981", "中芯国际"), ("002463", "沪电股份"), ("002384", "东山精密"),
        ("600745", "闻泰科技")]},
    {"code": "fb_ai", "name": "AI/算力", "stocks": [
        ("002230", "科大讯飞"), ("002415", "海康威视"), ("000063", "中兴通讯"),
        ("688111", "金山办公"), ("300059", "东方财富")]},
    {"code": "fb_newenergy", "name": "新能源", "stocks": [
        ("300750", "宁德时代"), ("002594", "比亚迪"), ("601012", "隆基绿能"),
        ("600438", "通威股份"), ("002460", "赣锋锂业"), ("300014", "亿纬锂能")]},
    {"code": "fb_baijiu", "name": "白酒消费", "stocks": [
        ("600519", "贵州茅台"), ("000858", "五粮液"), ("000568", "泸州老窖"),
        ("600809", "山西汾酒"), ("600887", "伊利股份")]},
    {"code": "fb_finance", "name": "金融地产", "stocks": [
        ("600036", "招商银行"), ("601318", "中国平安"), ("601166", "兴业银行"),
        ("600030", "中信证券"), ("000002", "万科A"), ("600048", "保利发展")]},
    {"code": "fb_medicine", "name": "医药", "stocks": [
        ("600276", "恒瑞医药"), ("300760", "迈瑞医疗"), ("600436", "片仔癀"),
        ("300015", "爱尔眼科"), ("000538", "云南白药")]},
    {"code": "fb_resource", "name": "资源能源", "stocks": [
        ("601899", "紫金矿业"), ("601088", "中国神华"), ("601225", "陕西煤业"),
        ("600028", "中国石化"), ("601857", "中国石油")]},
    {"code": "fb_consume", "name": "消费制造", "stocks": [
        ("002475", "立讯精密"), ("000651", "格力电器"), ("000333", "美的集团"),
        ("601633", "长城汽车"), ("002714", "牧原股份")]},
]

_CN_CACHE: dict = {}
_CN_UNIVERSE = None
_CN_ETF = None
_UNIVERSE_LOCK = threading.Lock()
_ETF_LOCK = threading.Lock()
_LINES_LOCK = threading.Lock()

# ===================== 指数（宽基 / 同花顺行业 / 同花顺概念） =====================
INDEX_BROAD = [
    {"code": "sh000001", "name": "上证指数", "kind": "broad"},
    {"code": "sz399001", "name": "深证成指", "kind": "broad"},
    {"code": "sz399006", "name": "创业板指", "kind": "broad"},
    {"code": "sh000300", "name": "沪深300", "kind": "broad"},
    {"code": "sh000905", "name": "中证500", "kind": "broad"},
    {"code": "sh000852", "name": "中证1000", "kind": "broad"},
    {"code": "sh000016", "name": "上证50", "kind": "broad"},
    {"code": "sh000688", "name": "科创50", "kind": "broad"},
    {"code": "sz399005", "name": "中小100", "kind": "broad"},
    {"code": "sz399330", "name": "深证100", "kind": "broad"},
    {"code": "sz399102", "name": "创业板综", "kind": "broad"},
    {"code": "sh000010", "name": "上证180", "kind": "broad"},
]

INDEX_ALIASES = {
    "上证": "上证指数", "大盘": "上证指数", "沪指": "上证指数",
    "深成指": "深证成指", "深指": "深证成指",
    "创业板": "创业板指", "科创": "科创50",
    "半导体设备": "半导体",
}

_INDEX_FALLBACK = {
    "industry": [
        {"code": "881121", "name": "半导体"}, {"code": "881129", "name": "通信设备"},
        {"code": "881272", "name": "软件开发"}, {"code": "881122", "name": "光学光电子"},
        {"code": "881130", "name": "计算机设备"}, {"code": "881117", "name": "通用设备"},
        {"code": "881118", "name": "专用设备"}, {"code": "881171", "name": "自动化设备"},
        {"code": "881278", "name": "电网设备"}, {"code": "881279", "name": "光伏设备"},
        {"code": "881280", "name": "风电设备"},
    ],
    "concept": [
        {"code": "308700", "name": "第三代半导体"},
        {"code": "308828", "name": "东数西算(算力)"},
        {"code": "309068", "name": "算力租赁"},
    ],
}
_IDX_LIST_CACHE: dict = {}
_IDX_KLINE_CACHE: dict = {}
_IDX_LOCK = threading.Lock()

# ===================== 美股搜索（Yahoo Finance + 常用中文别名） =====================
_US_POPULAR = [
    {"code": "AAPL", "name": "Apple Inc."},
    {"code": "MSFT", "name": "Microsoft Corp."},
    {"code": "NVDA", "name": "NVIDIA Corp."},
    {"code": "TSLA", "name": "Tesla Inc."},
    {"code": "AMZN", "name": "Amazon.com Inc."},
    {"code": "GOOGL", "name": "Alphabet Inc."},
    {"code": "META", "name": "Meta Platforms Inc."},
    {"code": "AVGO", "name": "Broadcom Inc."},
    {"code": "AMD", "name": "Advanced Micro Devices"},
    {"code": "NFLX", "name": "Netflix Inc."},
    {"code": "TSM", "name": "Taiwan Semiconductor"},
    {"code": "BABA", "name": "Alibaba Group"},
    {"code": "PDD", "name": "PDD Holdings"},
    {"code": "SPY", "name": "SPDR S&P 500 ETF"},
    {"code": "QQQ", "name": "Invesco QQQ Trust"},
    {"code": "SOXX", "name": "iShares Semiconductor ETF"},
    {"code": "SOXL", "name": "Direxion Semiconductor Bull 3X"},
    {"code": "TQQQ", "name": "ProShares UltraPro QQQ"},
]
_US_ALIAS = {
    "苹果": "AAPL", "微软": "MSFT", "英伟达": "NVDA", "特斯拉": "TSLA",
    "亚马逊": "AMZN", "谷歌": "GOOGL", "脸书": "META", "博通": "AVGO",
    "超微": "AMD", "奈飞": "NFLX", "台积电": "TSM", "阿里巴巴": "BABA",
    "拼多多": "PDD", "标普": "SPY", "标普500": "SPY", "纳斯达克": "QQQ",
    "纳指": "QQQ", "半导体": "SOXX", "三倍做多半导体": "SOXL",
}
_US_NAME_CACHE = {it["code"]: it["name"] for it in _US_POPULAR}
_US_SEARCH_CACHE = {}
_US_LOCK = threading.Lock()

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>回测游戏</title>
<style>
:root{--bg:#0e1116;--panel:#161b22;--panel2:#0b0f14;--border:#2b333d;--txt:#e8edf3;
      --dim:#94a0ad;--up:#e03c3c;--down:#1f9e8f;--accent:#4d9fff;--grid:#222a33}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     background:var(--bg);color:var(--txt);display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{height:50px;display:flex;align-items:center;gap:10px;padding:0 12px;
       border-bottom:1px solid var(--border);background:var(--panel2);flex-wrap:nowrap;overflow:visible;position:relative;z-index:20}
header h1{font-size:15px;margin:0;font-weight:700;white-space:nowrap}
select,input,button{font:inherit;background:var(--panel);color:var(--txt);border:1px solid var(--border);
       border-radius:7px;padding:5px 9px;font-size:13px}
button{cursor:pointer;white-space:nowrap}
button:hover{border-color:#3c4a59}
button.on,button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button:disabled{opacity:.4;cursor:default}
button.buy{background:var(--up);border-color:var(--up);color:#fff}
button.sell{background:var(--down);border-color:var(--down);color:#fff}
#wrap{flex:1;display:flex;min-height:0}
#sidebar{width:250px;background:var(--panel);border-right:1px solid var(--border);
         overflow-y:auto;padding:10px;font-size:12px}
#sidebar .sec{margin-bottom:14px}
#sidebar h3{margin:0 0 6px;font-size:12px;color:var(--dim);text-transform:uppercase}
#chartbox{flex:1;position:relative;min-width:0;display:flex;flex-direction:column}
canvas{display:block;width:100%;flex:1;min-height:0;cursor:crosshair;user-select:none;touch-action:none}
#status{height:26px;line-height:26px;padding:0 12px;flex:none;
        font-size:12px;color:var(--dim);border-top:1px solid var(--border);background:var(--panel2);
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#hud{position:absolute;right:10px;top:8px;font-size:12px;background:rgba(22,27,34,.85);
     padding:8px 10px;border:1px solid var(--border);border-radius:8px;line-height:1.7;
     pointer-events:none;font-variant-numeric:tabular-nums}
#hud b{color:var(--txt)} .pos{color:var(--up)} .neg{color:var(--down)}
#tip{position:absolute;display:none;background:rgba(22,27,34,.95);border:1px solid var(--border);
     border-radius:6px;padding:5px 8px;font-size:11.5px;line-height:1.5;pointer-events:none;
     z-index:6;color:var(--txt);font-variant-numeric:tabular-nums}
#list{max-height:160px;overflow-y:auto;border:1px solid var(--border);border-radius:6px}
#list div{padding:4px 6px;cursor:pointer;border-bottom:1px solid var(--border)}
#list div:hover{background:#1f2630}
#backtestedList{max-height:220px;overflow-y:auto;border:1px solid var(--border);border-radius:6px}
.btitem{padding:5px 6px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;gap:6px;align-items:center}
.btitem:hover{background:#1f2630}
.btitem .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.btitem .c{color:var(--dim);font-size:11px}
.btitem .r{font-weight:700;font-variant-numeric:tabular-nums}
.btitem .del{color:var(--dim);padding:0 4px;font-size:15px;line-height:1;border-radius:4px;flex:none}
.btitem .id{font-size:10px;color:var(--accent);border:1px solid var(--border);border-radius:4px;
            padding:0 3px;flex:none;line-height:1.5;cursor:pointer}
.btitem .id:hover{background:#26303b}
#saveId b{color:var(--accent);font-family:ui-monospace,SFMono-Regular,monospace}
.btitem .del:hover{color:#fff;background:var(--up)}
.btempty{padding:5px 6px;color:var(--dim)}
/* 估值面板 */
.vrow{display:flex;align-items:center;gap:6px;padding:2px 0;font-variant-numeric:tabular-nums}
.vrow .k{width:26px;color:var(--dim);flex:none}
.vrow .v{flex:1;text-align:right}
.vbadge{min-width:42px;text-align:center;border-radius:4px;padding:1px 5px;font-size:11px;
        font-weight:700;flex:none}
.vg{background:rgba(47,191,113,.16);color:#3ddc84}
.vy{background:rgba(242,201,76,.16);color:#f2c94c}
.vr{background:rgba(224,60,60,.16);color:#ff6b6b}
.vn{background:rgba(148,160,173,.13);color:var(--dim)}
.vlegend{display:flex;gap:10px;margin-top:5px;color:var(--dim);font-size:11px;align-items:center}
.vlegend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:3px}
.combo{position:relative;display:inline-block;width:145px}
.combo input{width:100%;padding-right:6px}
.menu{position:absolute;left:0;top:calc(100% + 4px);min-width:230px;max-height:300px;overflow-y:auto;
      background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:3px;
      box-shadow:0 10px 30px rgba(0,0,0,.55);display:none;z-index:50}
.menu .opt{padding:5px 8px;border-radius:5px;cursor:pointer;display:flex;gap:8px;align-items:center}
.menu .opt:hover,.menu .opt.on{background:#26303b}
.menu .opt .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.menu .opt .cd{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
.menu .empty{padding:6px 8px;color:var(--dim)}
.maGrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 8px}
.maGrid label{display:flex;align-items:center;gap:5px;margin:0;color:var(--txt);font-size:12px}
.maGrid input{margin:0;accent-color:var(--accent)}
.modalMask{position:fixed;inset:0;background:rgba(0,0,0,.58);display:none;align-items:center;justify-content:center;z-index:100}
.modal{width:min(420px,calc(100vw - 32px));background:var(--panel);border:1px solid var(--border);border-radius:10px;
       box-shadow:0 20px 60px rgba(0,0,0,.65);padding:16px}
.modal h3{margin:0 0 8px;font-size:14px}
.modal .msg{white-space:pre-line;color:var(--txt);margin:0 0 16px;line-height:1.7;font-size:13px}
.modalBtns{display:flex;justify-content:flex-end;gap:8px}
.toolbtn{display:flex;gap:5px;flex-wrap:wrap}
.toolbtn button{flex:1;min-width:52px}
label{display:block;margin:4px 0 2px;color:var(--dim)}
#tradeLog{margin-top:8px;max-height:120px;overflow-y:auto;color:var(--dim)}
#tradeStats{margin-top:8px;color:var(--txt);line-height:1.7}
</style>
</head>
<body>
<header>
  <h1>回测游戏</h1>
  <label>市场<select id="market">
    <option value="cn">A股</option><option value="us">美股</option><option value="idx">指数</option><option value="cmd">商品</option>
  </select></label>
  <label><span id="boardLabel">板块</span>
    <div class="combo"><input id="boardInput" placeholder="中文/代码" autocomplete="off" spellcheck="false">
      <div class="menu" id="boardMenu"></div></div>
  </label>
  <label>标的
    <div class="combo"><input id="symbolInput" placeholder="中文/代码" autocomplete="off" spellcheck="false">
      <div class="menu" id="symbolMenu"></div></div>
  </label>
  <label>开始日期<select id="start"></select></label>
  <label>速度<select id="speed">
    <option value="1500">慢</option><option value="700" selected>中</option>
    <option value="250">快</option><option value="80">极快</option>
  </select></label>
  <label>自动 <input type="checkbox" id="autoPlay"></label>
  <button id="btnPause" disabled>⏹ 停止</button>
  <button id="btnPrev" disabled>◀ 上一步</button>
  <button id="btnStep" disabled>下一步</button>
  <button id="btnReset">↺ 重置</button>
</header>
<div id="wrap">
  <div id="sidebar">
    <div class="sec">
      <h3>交易</h3>
      <label>操作单位 <select id="unitMode">
        <option value="half">半日（09:55 / 14:55）</option>
        <option value="day">一日（收盘）</option>
        <option value="week">周线（周收盘）</option>
      </select></label>
      <div id="unitHint" style="margin-top:6px;color:var(--dim)">半日：09:55 = 开盘价，14:55 = 收盘价</div>
      <div style="display:flex;gap:5px;margin-top:8px">
        <button class="buy" id="btnBuy" style="flex:1">买入</button>
        <button class="sell" id="btnSell" style="flex:1">卖出</button>
        <button class="primary" id="btnStart" style="flex:1.7;font-size:12px;padding:5px 4px" disabled>▶ 开始回放</button>
      </div>
      <label style="margin-top:8px">买卖比例 <select id="buySize">
        <option value="1">全部</option><option value="0.5">半仓</option>
        <option value="0.25">1/4仓</option>
      </select></label>
      <button id="btnSave" style="width:100%;margin-top:6px" disabled>💾 保存交易过程</button>
      <div id="saveId" style="display:none;margin-top:5px;color:var(--dim);font-size:11px;line-height:1.6"></div>
      <div id="tradeLog"></div>
      <div id="tradeStats"></div>
    </div>
    <div class="sec">
      <h3>至今表现</h3>
      <div id="perfStats" style="line-height:1.9">--</div>
      <div style="margin-top:4px;color:var(--dim)">波动率见右上角最大回撤一行<br>至少 30 个交易日才显示</div>
    </div>
    <div class="sec">
      <h3>均线</h3>
      <div class="maGrid">
        <label><input type="checkbox" id="ma5" checked><span style="color:#f2c94c">MA5</span></label>
        <label><input type="checkbox" id="ma17"><span style="color:#ff8c42">MA17</span></label>
        <label><input type="checkbox" id="ma21" checked><span style="color:#c77dff">MA21</span></label>
        <label><input type="checkbox" id="ma42"><span style="color:#4dd0e1">MA42</span></label>
      </div>
      <div style="margin-top:6px;color:var(--dim)">周期按当前操作单位计算</div>
      <label style="display:flex;align-items:center;gap:6px;margin-top:7px">
        <input type="checkbox" id="logScale"> 对数坐标
      </label>
    </div>
    <div class="sec">
      <h3>波动性</h3>
      <div id="volStats" style="line-height:1.8;color:var(--txt)">--</div>
      <div style="margin-top:4px;color:var(--dim)">HV = 当前单位20周期年化波动率<br>ATR = 20日平均真实波幅</div>
    </div>
    <div class="sec">
      <h3>仓位参考（波动率目标）</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <label style="margin:0">目标波动
          <select id="vtTarget" style="width:100%;margin-top:2px">
            <option value="0.15">15%</option>
            <option value="0.2" selected>20%</option>
            <option value="0.25">25%</option>
            <option value="0.3">30%</option>
          </select>
        </label>
        <label style="margin:0">回看窗口
          <select id="vtLB" style="width:100%;margin-top:2px">
            <option value="60">60 日</option>
            <option value="80" selected>80 日</option>
            <option value="120">120 日</option>
          </select>
        </label>
      </div>
      <div id="vtStats" style="line-height:1.9;margin-top:7px">--</div>
      <div style="margin-top:4px;color:var(--dim)">建议仓位 = min(1, 目标波动 ÷ 年化波动)<br>始终做多，只调仓位；不自动交易</div>
    </div>
    <div class="sec">
      <h3>估值</h3>
      <div id="valStats" style="line-height:1.75">--</div>
      <div class="vlegend">
        <span><i style="background:#3ddc84"></i>便宜</span>
        <span><i style="background:#f2c94c"></i>中性</span>
        <span><i style="background:#ff6b6b"></i>偏贵</span>
      </div>
      <div style="margin-top:4px;color:var(--dim)">分位 = 截至当日向前 5 年<br>绿 &lt;30% · 黄 30–70% · 红 &gt;70%<br>只用当日及之前数据，无未来函数</div>
    </div>
    <div class="sec">
      <h3>画线</h3>
      <div class="toolbtn">
        <button id="bT">趋势线</button>
        <button id="bV">纵向线</button>
      </div>
      <div class="toolbtn" style="margin-top:5px">
        <button id="bUndo" disabled>撤销</button>
        <button id="bRedo" disabled>重做</button>
        <button id="bDel" disabled>删除</button>
        <button id="bClr">清空</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:7px">
        <label style="margin:0">趋势线上限
          <input id="limitTrend" type="number" min="1" max="100" value="20" style="width:100%;margin-top:2px">
        </label>
        <label style="margin:0">纵向线上限
          <input id="limitV" type="number" min="1" max="100" value="5" style="width:100%;margin-top:2px">
        </label>
      </div>
      <div id="lineHint" style="margin-top:6px;color:var(--dim)">趋势线最多20条 · 纵向线最多5条（新增自动去掉最旧）</div>
      <div style="margin-top:2px;color:var(--dim)">拖拽平移 · 滚轮缩放 · 右键/Esc退出画线 · Delete删线</div>
    </div>
    <div class="sec">
      <h3>自选标的</h3>
      <input id="codeInput" placeholder="输入代码/中文名 如 300750 / 茅台 / SOXL" style="width:100%">
      <button id="btnLoad" style="width:100%;margin-top:4px">加载标的</button>
      <div id="list" style="margin-top:6px"></div>
    </div>
    <div class="sec">
      <h3>已回测</h3>
      <div id="backtestedList" style="margin-top:4px"></div>
    </div>
  </div>
  <div id="chartbox">
    <canvas id="cv"></canvas>
    <div id="status">载入中…</div>
    <div id="tip"></div>
    <div id="hud">
      时点 <b id="hDate">--</b><br>
      你的资金 <b id="hEquity">--</b><br>
      基准资金 <b id="hBench">--</b><br>
      你的收益 <b id="hRet">--</b><br>
      基准收益 <b id="hBenchRet">--</b><br>
      你的最大回撤 <b id="hDD">--</b> · 波动 <b id="hVol">--</b><br>
      基准最大回撤 <b id="hBenchDD">--</b> · 波动 <b id="hBenchVol">--</b><br>
      当前价 <b id="hPrice">--</b> · 持仓 <b id="hPos">0</b>
    </div>
  </div>
</div>
<div id="confirmMask" class="modalMask" role="dialog" aria-modal="true">
  <div class="modal">
    <h3 id="confirmTitle">请确认</h3>
    <div class="msg" id="confirmText"></div>
    <div class="modalBtns">
      <button id="confirmCancel">取消</button>
      <button class="primary" id="confirmOk">确定</button>
    </div>
  </div>
</div>
<script>
"use strict";

const $=id=>document.getElementById(id);
const cv=$('cv'), ctx=cv.getContext('2d');

/* ===================== 颜色 / 常量 ===================== */
const UP="#e03c3c", DOWN="#1f9e8f", ACCENT="#4d9fff", MUTED="#94a0ad";
const LINE_NAMES={h:'水平线',trend:'趋势线',v:'纵向线'};
const MA_DEFS=[{p:5,color:"#f2c94c"},{p:17,color:"#ff8c42"},{p:21,color:"#c77dff"},{p:42,color:"#4dd0e1"}];
const FEE=0.0005;               // 双边手续费 0.05%
const IDX_GROUPS=[
  {code:"idx_broad",name:"宽基指数"},
  {code:"idx_industry",name:"行业指数"},
  {code:"idx_concept",name:"概念指数"},
];

/* ===================== 状态 ===================== */
const S={
  market:"cn", code:"", name:"", bars:[], days:[], tf:"1d", unitMode:"half",
  boards:[], boardCode:"", symbols:[], symbolCode:"", idxKind:"",
  allLines:{}, selected:null, mode:null, p1:null,
  lineLimitTrend:20, lineLimitV:5,
  view:{l:0,r:0}, ylim:null, hover:null, drag:null, pan:null, userView:false,
  volDrag:null, logScale:false,
  undo:[], redo:[],
  // 游戏
  reveal:0, startIdx:0, playing:false, timer:null, viewingSaved:false,
  ma:{5:true,17:false,21:true,42:false},
  cash:100000, shares:0, trades:[], benchPrice:null, openTrade:null, roundTrips:[],
  peakEquity:100000, maxDD:0, benchPeak:100000, benchMaxDD:0,
  customList:[],
  val:{rows:[], byDate:null, reason:'', code:''},
  vt:{target:0.20, lb:80},
  history:[], eqHist:[], saved:false,
};

/* ===================== 格式化 ===================== */
function fmt(n,d=2){return Number(n).toFixed(d)}
function fmtAxis(v){return fmt(v,v>=1000?0:(v>=10?2:3))}
function fmtPct(v){return (v>=0?'+':'')+Number(v).toFixed(2)+'%'}
function pctColor(v){return v>0?UP:(v<0?DOWN:MUTED)}
function pct(a,b){return (a/b-1)*100}
function round4(v){return Math.round(v*10000)/10000}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function copyText(t){
  try{await navigator.clipboard.writeText(t);return true;}catch(_){}
  try{
    const ta=document.createElement('textarea');ta.value=t;
    ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    const ok=document.execCommand('copy');ta.remove();return ok;
  }catch(_){return false;}
}
function showTradeId(id,prefix){
  const el=$('saveId');
  if(!el)return;
  if(!id){el.style.display='none';el.innerHTML='';return;}
  el.style.display='block';
  el.innerHTML=`${esc(prefix||'交易 ID')}：<b>${esc(id)}</b> `+
    `<button id="btnCopyId" style="padding:1px 5px;font-size:11px">复制</button>`;
  $('btnCopyId').onclick=async()=>{
    const ok=await copyText(id);
    setStatus(ok?('已复制交易 ID：'+id):('交易 ID：'+id));
  };
}
function fmtShares(n){
  n=Math.round(n||0);
  if(n>=10000){const w=n/10000;return (w%1===0?w.toFixed(0):w.toFixed(1))+'万'}
  return String(n);
}
function fmtVol(n){
  n=n||0;
  if(n>=1e8)return (n/1e8).toFixed(2)+'亿';
  if(n>=1e4)return (n/1e4).toFixed(1)+'万';
  return String(Math.round(n));
}

/* ===================== 操作单位（半日 / 一日 / 周线） ===================== */
const SLOT_LABEL=['09:55','14:55'];
const UNIT_NAME={half:'半日',day:'一日',week:'周线'};
function viewWindow(){return S.unitMode==='half'?720:(S.unitMode==='day'?360:52);}
function tfForUnit(m){return m==='day'?'1d_day':(m==='week'?'1d_week':'1d');}
function unitText(b){
  if(S.unitMode==='day')return '收盘';
  if(S.unitMode==='week')return '周收盘';
  return SLOT_LABEL[b.slot]||'';
}
// 日K -> 当前操作单位的时点
// half: 每天 09:55(开盘) / 14:55(收盘)
// day : 每天收盘
// week: 每周最后一个交易日的收盘
function buildTicks(days,mode){
  mode=mode||S.unitMode;
  if(mode==='day'){
    return days.map((b,di)=>({
      t:b.t+12*3600*1000, day:di, slot:1,
      o:b.o, h:b.h, l:b.l, c:b.c, v:b.v,
    }));
  }
  if(mode==='week'){
    const out=[];
    let cur=null;
    const emit=()=>{
      if(!cur)return;
      out.push({
        t:cur.t+12*3600*1000, day:cur.day, slot:1, week:out.length,
        o:cur.o, h:cur.h, l:cur.l, c:cur.c, v:cur.v,
      });
      cur=null;
    };
    days.forEach((b,di)=>{
      const d=new Date(b.t);
      const dow=(d.getUTCDay()+6)%7; // 0=周一
      const monday=Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate()-dow);
      if(cur&&cur.key!==monday)emit();
      if(!cur){
        cur={key:monday,o:b.o,h:b.h,l:b.l,c:b.c,v:+b.v||0,day:di,t:b.t};
      }else{
        cur.h=Math.max(cur.h,b.h);cur.l=Math.min(cur.l,b.l);
        cur.c=b.c;cur.v+=(+b.v||0);cur.day=di;cur.t=b.t;
      }
    });
    emit();
    return out;
  }
  const out=[];
  days.forEach((b,di)=>{
    for(let slot=0;slot<2;slot++){
      out.push({
        t:b.t+slot*12*3600*1000, day:di, slot,
        o:b.o, h:b.h, l:b.l,
        c:slot===0?b.o:b.c,
        v:slot===1?b.v:0, dayV:b.v,
      });
    }
  });
  return out;
}
function tickLabel(b){
  const d=new Date(b.t),p=v=>String(v).padStart(2,'0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} ${unitText(b)}`;
}
function tickLabelShort(b){
  const d=new Date(b.t),p=v=>String(v).padStart(2,'0');
  return `${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} ${unitText(b)}`;
}

/* ===================== 可搜索下拉框（板块 / 标的） ===================== */
let boardCombo=null,symbolCombo=null;
function comboNorm(s){return String(s==null?'':s).toLowerCase().replace(/[\s\u3000]+/g,'')}
function comboFilter(list,q){
  const k=comboNorm(q);
  if(!k)return list.slice();
  return list.filter(it=>comboNorm(it.name).includes(k)||String(it.code).toLowerCase().includes(k));
}
function makeCombo(inputEl,menuEl,opts){
  let items=[],active=-1,seq=0,timer=null;
  const visible=()=>menuEl.style.display==='block';
  function paint(note){
    menuEl.innerHTML='';
    if(!items.length){
      const e=document.createElement('div');e.className='empty';
      e.textContent=note||'无匹配';menuEl.appendChild(e);
    }else{
      items.forEach((it,i)=>{
        const d=document.createElement('div');
        d.className='opt'+(i===active?' on':'');
        const nm=document.createElement('span');nm.className='nm';nm.textContent=it.name;
        const cd=document.createElement('span');cd.className='cd';cd.textContent=opts.sub?opts.sub(it):it.code;
        d.appendChild(nm);d.appendChild(cd);
        d.addEventListener('mousedown',ev=>{ev.preventDefault();choose(i);});
        menuEl.appendChild(d);
      });
    }
    menuEl.style.display='block';
  }
  function hide(){menuEl.style.display='none';active=-1;}
  function choose(i){
    const it=items[i];if(!it)return;
    inputEl.value=it.name;
    hide();
    opts.onPick(it);
  }
  function move(dir){
    if(!items.length)return;
    active=(active+dir+items.length)%items.length;
    [...menuEl.children].forEach((el,i)=>el.classList.toggle('on',i===active));
    const el=menuEl.children[active];
    if(el&&el.scrollIntoView)el.scrollIntoView({block:'nearest'});
  }
  function run(q,showAll){
    const my=++seq;
    clearTimeout(timer);
    items=opts.local(q);
    active=items.length?0:-1;
    const remote=typeof opts.remote==='function'?opts.remote():opts.remote;
    if(!remote||!q){paint();return;}
    paint('搜索中…');
    timer=setTimeout(async()=>{
      try{
        const r=await api(remote+encodeURIComponent(q));
        if(my!==seq)return;
        const keyOf=opts.key||(it=>it.code);
        const seen=new Set(),merged=[];
        for(const it of [...items,...(r||[])]){
          const k=keyOf(it);
          if(!seen.has(k)){seen.add(k);merged.push(it);}
        }
        items=merged.slice(0,40);active=items.length?0:-1;
        paint();
      }catch(e){if(my===seq)paint('搜索失败');}
    },180);
  }
  inputEl.addEventListener('focus',()=>{inputEl.select();run('',true);});
  inputEl.addEventListener('input',()=>run(inputEl.value.trim(),false));
  inputEl.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){e.preventDefault();if(!visible())run(inputEl.value.trim(),true);else move(1);}
    else if(e.key==='ArrowUp'){e.preventDefault();move(-1);}
    else if(e.key==='Enter'){
      e.preventDefault();
      if(visible()&&items.length)choose(active>=0?active:0);
      else if(opts.onEnter)opts.onEnter(inputEl.value.trim());
    }else if(e.key==='Escape'){hide();}
  });
  inputEl.addEventListener('blur',()=>setTimeout(hide,120));
  return {
    setItems(list){items=list.slice();active=items.length?0:-1;},
    setValue(v){inputEl.value=v||'';},
    open(){inputEl.focus();},
    hide,
  };
}

/* ===================== 画布几何（对齐 sector_monitor_web） ===================== */
const G={w:0,h:0,pad:{l:54,r:14,t:14,b:22},mainH:0,volH:0,volTop:0,dpr:1,volRatio:0.16};
function layoutPanes(){
  const avail=Math.max(80,G.h-G.pad.t-G.pad.b-4);
  const minMain=Math.min(140,Math.max(60,avail*0.35));
  const maxVol=Math.max(30,avail-minMain);
  let vh=Math.round(G.h*G.volRatio);
  vh=Math.max(35,Math.min(vh,maxVol));
  G.volH=vh;
  G.mainH=avail-vh;
  G.volTop=G.pad.t+G.mainH+4;
}
function resize(){
  G.dpr=window.devicePixelRatio||1;
  G.w=Math.max(200,cv.clientWidth);
  G.h=Math.max(160,cv.clientHeight);
  cv.width=Math.round(G.w*G.dpr);
  cv.height=Math.round(G.h*G.dpr);
  ctx.setTransform(G.dpr,0,0,G.dpr,0,0);
  layoutPanes();
  draw();
}
const plotW=()=>Math.max(10,G.w-G.pad.l-G.pad.r);
const nBars=()=>S.bars.length;
const livePrice=()=>nBars()?S.bars[Math.min(S.reveal,nBars()-1)].c:0;

function x2px(i){return G.pad.l+(i-S.view.l)/(S.view.r-S.view.l)*plotW();}
function px2x(px){return S.view.l+(px-G.pad.l)/plotW()*(S.view.r-S.view.l);}
function p2y(p){
  const yl=S.ylim||{lo:0,hi:1};
  if(S.logScale){
    const lo=Math.log(Math.max(yl.lo,1e-9)),hi=Math.log(Math.max(yl.hi,1e-9));
    const v=Math.log(Math.max(p,1e-9));
    return G.pad.t+(hi-v)/((hi-lo)||1)*G.mainH;
  }
  return G.pad.t+(yl.hi-p)/((yl.hi-yl.lo)||1)*G.mainH;
}
function y2p(py){
  const yl=S.ylim||{lo:0,hi:1};
  if(S.logScale){
    const lo=Math.log(Math.max(yl.lo,1e-9)),hi=Math.log(Math.max(yl.hi,1e-9));
    return Math.exp(hi-(py-G.pad.t)/G.mainH*(hi-lo));
  }
  return yl.hi-(py-G.pad.t)/G.mainH*(yl.hi-yl.lo);
}

/* 时间 <-> 索引（与 sector_monitor 的 dt2x/x2dt 一致） */
function dt2x(ms){
  const b=S.bars;
  if(b.length<2)return 0;
  const a0=b[0].t,a1=b[b.length-1].t;
  if(a1===a0)return 0;
  return (Number(ms)-a0)/(a1-a0)*(b.length-1);
}
function x2dt(x){
  const b=S.bars;
  if(!b.length)return Date.now();
  if(b.length<2)return b[0].t;
  const a0=b[0].t,a1=b[b.length-1].t;
  return a0+(a1-a0)*(x/(b.length-1));
}
function isoFromMs(ms){return new Date(ms).toISOString();}

function visibleIdx(){
  const n=nBars();
  let i0=Math.max(0,Math.floor(S.view.l));
  let i1=Math.min(n-1,Math.ceil(S.view.r),S.reveal);
  if(i1<i0)i1=i0;
  return [i0,i1];
}
function autoscale(i0,i1){
  if(!nBars()){S.ylim={lo:0,hi:1};return;}
  let lo=Infinity,hi=-Infinity;
  for(let i=i0;i<=i1;i++){const c=S.bars[i].c;if(c<lo)lo=c;if(c>hi)hi=c;}
  if(!isFinite(lo)||!isFinite(hi)){S.ylim={lo:0,hi:1};return;}
  if(S.logScale&&lo>0){
    const llo=Math.log(lo),lhi=Math.log(hi);
    const lp=(lhi-llo)*0.08||0.01;
    S.ylim={lo:Math.exp(llo-lp),hi:Math.exp(lhi+lp)};
  }else{
    const pad=(hi-lo)*0.08||Math.max(Math.abs(hi)*0.01,0.01);
    S.ylim={lo:lo-pad,hi:hi+pad};
  }
}
function resetView(){
  if(!nBars())return;
  S.view={l:Math.max(-1,S.reveal-viewWindow()),r:S.reveal};
  S.userView=false;
  const [i0,i1]=visibleIdx();
  autoscale(i0,i1);
}

/* ===================== 数据 ===================== */
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
function setStatus(m){$('status').textContent=m}

boardCombo=makeCombo($('boardInput'),$('boardMenu'),{
  local:q=>comboFilter(S.boards,q),
  onPick:it=>selectBoard(it.code),
  sub:it=>{
    if(String(it.code).startsWith('idx_'))return '指数';
    return String(it.code).startsWith('fb_')?'内置':it.code;
  },
});
symbolCombo=makeCombo($('symbolInput'),$('symbolMenu'),{
  local:q=>comboFilter(S.symbols,q),
  remote:()=>{
    if(S.market==='cn')return '/api/search?market=cn&q=';
    if(S.market==='idx')return '/api/search_index?q=';
    if(S.market==='us')return '/api/search_us?q=';
    if(S.market==='cmd')return '/api/search_cmd?q=';
    return null;
  },
  key:it=>(it.kind||it.market||'')+':'+it.code,
  sub:it=>{
    if(it.market==='idx')return /[指]/.test(it.name)?'':'指数';
    if(it.market==='cmd')return it.unit||'商品';
    return it.code;
  },
  onPick:it=>{
    if(it.market==='cmd'||S.market==='cmd')selectSymbol(it.code,it.name);
    else if(it.market==='idx')selectIndexResult(it);
    else if(S.market==='idx')selectIndex(it);
    else selectSymbol(it.code,it.name);
  },
  onEnter:q=>{
    if(!q)return;
    if(S.market==='idx')resolveIndex(q);
    else if(S.market==='us')resolveUS(q);
    else if(S.market==='cmd')resolveCmd(q);
    else if(S.market==='cn'&&/[\u4e00-\u9fa5]/.test(q))resolveSymbol(q);
    else if(/^[A-Za-z0-9.\-]+$/.test(q))loadKline(q,'');
    else setStatus('未找到: '+q);
  },
});

async function selectBoard(code){
  const b=S.boards.find(x=>x.code===code);
  if(!b)return false;
  S.boardCode=code;
  boardCombo.setValue(b.name);
  return await loadConstituents(code);
}
async function selectSymbol(code,name){
  S.symbolCode=code;
  symbolCombo.setValue(name||code);
  return await loadKline(code,name);
}
async function selectIndex(it){
  S.idxKind=it.kind||'broad';
  S.symbolCode=it.code;
  symbolCombo.setValue(it.name);
  return await loadKline(it.code,it.name,it.kind);
}
async function selectIndexResult(it){
  const kind=it.idxKind||'broad';
  applyMarketUI('idx');
  S.boards=IDX_GROUPS.slice();
  boardCombo.setItems(S.boards);
  const grp=kind==='broad'?'idx_broad':kind==='ind'?'idx_industry':'idx_concept';
  S.boardCode=grp;
  const g=S.boards.find(x=>x.code===grp);
  boardCombo.setValue(g?g.name:'');
  try{
    const group=kind==='broad'?'broad':kind==='ind'?'industry':'concept';
    const j=await api('/api/index_list?group='+group);
    S.symbols=j||[];
    symbolCombo.setItems(S.symbols);
  }catch(e){S.symbols=[];symbolCombo.setItems([]);}
  return await selectIndex({code:it.code,name:it.name,kind});
}
async function resolveSymbol(q){
  setStatus('搜索中… '+q);
  try{
    const r=await api('/api/search?market=cn&q='+encodeURIComponent(q));
    if(r&&r.length){
      const it=r[0];
      if(it.market==='idx')selectIndexResult(it);
      else selectSymbol(it.code,it.name);
    }else setStatus('未找到: '+q);
  }catch(e){setStatus('搜索失败: '+e.message)}
}
async function resolveIndex(q){
  setStatus('搜索指数… '+q);
  try{
    const r=await api('/api/search_index?q='+encodeURIComponent(q));
    if(r&&r.length)selectIndex(r[0]);
    else setStatus('未找到指数: '+q);
  }catch(e){setStatus('搜索指数失败: '+e.message)}
}
async function resolveUS(q){
  setStatus('搜索美股… '+q);
  try{
    const r=await api('/api/search_us?q='+encodeURIComponent(q));
    if(r&&r.length){selectSymbol(r[0].code,r[0].name);return;}
    if(/^[A-Za-z0-9.\-]+$/.test(q)){loadKline(q.toUpperCase(),'');return;}
    setStatus('未找到美股: '+q+'（可输入 ticker 或英文公司名）');
  }catch(e){setStatus('美股搜索失败: '+e.message)}
}
async function resolveCmd(q){
  setStatus('搜索商品… '+q);
  try{
    const r=await api('/api/search_cmd?q='+encodeURIComponent(q));
    if(r&&r.length){selectSymbol(r[0].code,r[0].name);return;}
    setStatus('未找到商品: '+q);
  }catch(e){setStatus('商品搜索失败: '+e.message)}
}
async function loadIdxBoards(){
  S.boards=IDX_GROUPS.slice();
  boardCombo.setItems(S.boards);
  await selectBoard(S.boards[0].code);
}
async function loadCmdList(){
  S.boards=[];
  boardCombo.setItems([]);
  S.boardCode='';
  $('boardInput').value='商品';
  try{
    const j=await api('/api/cmd_list');
    S.symbols=j||[];
    symbolCombo.setItems(S.symbols);
    if(S.symbols.length)await selectSymbol(S.symbols[0].code,S.symbols[0].name);
    else setStatus('商品列表为空');
  }catch(e){setStatus('商品加载失败: '+e.message)}
}
async function loadBoards(){
  if(S.market==='idx')return loadIdxBoards();
  if(S.market==='cmd')return loadCmdList();
  try{
    const j=await api('/api/boards');
    S.boards=j||[];
    boardCombo.setItems(S.boards);
    let ok=false;
    for(const b of S.boards.slice(0,6)){
      ok=await selectBoard(b.code);
      if(ok)break;
    }
    if(!ok){
      S.boardCode='';boardCombo.setValue('');
      const loaded=await selectSymbol('600519','贵州茅台');
      setStatus(loaded
        ? '板块暂无成分，已加载示例 贵州茅台；可直接在「标的」搜索其他股票'
        : '板块/数据加载失败，请稍后重试或直接输入代码');
    }
  }catch(e){setStatus('板块加载失败: '+e.message)}
}
async function loadConstituents(boardCode){
  const code=boardCode||S.boardCode;if(!code)return false;
  if(S.market==='idx'){
    const group=String(code).replace('idx_','');
    try{
      const j=await api('/api/index_list?group='+encodeURIComponent(group));
      S.symbols=j||[];
      symbolCombo.setItems(S.symbols);
      if(S.symbols.length)return await selectIndex(S.symbols[0]);
      S.symbolCode='';symbolCombo.setValue('');
      setStatus('该分类暂无指数');
      return false;
    }catch(e){setStatus('加载指数列表失败: '+e.message);return false}
  }
  try{
    const j=await api('/api/constituents?board='+encodeURIComponent(code));
    S.symbols=j||[];
    symbolCombo.setItems(S.symbols);
    if(S.symbols.length)return await selectSymbol(S.symbols[0].code,S.symbols[0].name);
    S.symbolCode='';symbolCombo.setValue('');
    setStatus('该板块暂无成分，可直接输入代码/中文名搜索');
    return false;
  }catch(e){setStatus('加载成分失败: '+e.message);return false}
}
async function loadKline(code,name,kindArg){
  const idxKind=S.market==='idx'?(kindArg||S.idxKind||'broad'):'';
  let url='/api/kline?code='+encodeURIComponent(code);
  if(idxKind)url+='&market=idx&kind='+encodeURIComponent(idxKind);
  else if(S.market==='cmd')url+='&market=cmd';
  try{
    const j=await api(url);
    if(!j.bars||!j.bars.length){setStatus('无数据: '+(name||code));return false}
    S.days=j.bars.map(b=>({t:new Date(b[0]).getTime(),o:+b[1],h:+b[2],l:+b[3],c:+b[4],v:+b[5]}));
    S.bars=buildTicks(S.days,S.unitMode);
    S.code=code;S.tf=tfForUnit(S.unitMode);
    S.name=(j.name&&j.name!==code)?j.name:(name||j.name||code);
    if(idxKind)S.idxKind=idxKind;
    S.symbolCode=code;
    if(symbolCombo)symbolCombo.setValue(S.name);
    S.selected=null;S.mode=null;S.p1=null;S.undo=[];S.redo=[];
    if(S.market!=='idx'&&S.market!=='cmd')addCustom(code,S.name);
    fillStartDates();showFull();
    await loadLines();
    updateButtons();
    loadValuation(code);
    return true;
  }catch(e){setStatus('加载失败: '+e.message);return false}
}
function fillStartDates(){
  const sel=$('start');sel.innerHTML='';
  if(!S.bars.length)return;
  const step=S.unitMode==='half'?10:(S.unitMode==='day'?5:4);
  for(let i=0;i<S.bars.length;i+=step){
    const o=document.createElement('option');
    o.value=i;
    o.textContent=new Date(S.bars[i].t).toISOString().slice(0,10);
    sel.appendChild(o);
  }
  const last=S.bars.length-1;
  const def=Math.max(0,last-viewWindow());
  const pick=Math.min(last,Math.floor(def/step)*step);
  sel.value=String(pick);
}
function addCustom(code,name){
  if(!S.customList.find(x=>x.code===code))S.customList.unshift({code,name:name||code});
  const d=$('list');d.innerHTML='';
  S.customList.forEach(x=>{const e=document.createElement('div');
    e.textContent=x.name+' '+x.code;e.onclick=()=>loadKline(x.code,x.name);d.appendChild(e)});
}

/* ===================== 画线（对齐 sector_monitor_web） ===================== */
const lineKey=()=>S.market.toUpperCase()+":"+S.code+"|"+S.tf;
function curLines(){
  const k=lineKey();
  if(!Array.isArray(S.allLines[k]))S.allLines[k]=[];
  return S.allLines[k];
}
function classify(y){return livePrice()>=y?DOWN:UP;}

function lineEndpoints(ln){
  if(ln.type==="h")return [[S.view.l,ln.y],[S.view.r,ln.y]];
  if(ln.type==="v"){const x=dt2x(Date.parse(ln.x));return [[x,ln.y1],[x,ln.y2]];}
  return [[dt2x(Date.parse(ln.x1)),ln.y1],[dt2x(Date.parse(ln.x2)),ln.y2]];
}
function yAtNow(ln){
  if(ln.type==="h")return ln.y;
  if(ln.type==="v")return NaN;
  const x1=dt2x(Date.parse(ln.x1)),x2=dt2x(Date.parse(ln.x2));
  if(Math.abs(x2-x1)<1e-9)return ln.y2;
  return ln.y1+(ln.y2-ln.y1)*(S.reveal-x1)/(x2-x1);
}

function drawLines(){
  const arr=curLines();
  if(S.selected!==null&&S.selected>=arr.length)S.selected=null;
  arr.forEach((ln,idx)=>{
    if(ln.type==="h")return;      // 已取消水平线
    const sel=idx===S.selected;
    const col=classify(yAtNow(ln));
    const lw=sel?2.4:1.3;
    ctx.save();
    ctx.strokeStyle=col;ctx.lineWidth=lw;ctx.setLineDash([6,4]);
    ctx.beginPath();
    if(ln.type==="v"){
      const px=x2px(dt2x(Date.parse(ln.x)));
      if(px<G.pad.l-1||px>G.w-G.pad.r+1){ctx.restore();return;}
      const py1=p2y(ln.y1),py2=p2y(ln.y2);
      const pc=ln.y1?(ln.y2/ln.y1-1)*100:0;
      ctx.strokeStyle=pctColor(pc);
      ctx.beginPath();ctx.moveTo(px,py1);ctx.lineTo(px,py2);ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();ctx.moveTo(px-5,py1);ctx.lineTo(px+5,py1);
      ctx.moveTo(px-5,py2);ctx.lineTo(px+5,py2);ctx.stroke();
      const lab=fmtPct(pc);
      ctx.font="700 11px sans-serif";
      const tw=ctx.measureText(lab).width;
      const cy=(py1+py2)/2;
      ctx.fillStyle="#0e1116";ctx.fillRect(px+7,cy-8,tw+9,16);
      ctx.fillStyle=pctColor(pc);ctx.textAlign="left";ctx.textBaseline="middle";
      ctx.fillText(lab,px+11,cy);
      if(Math.abs(py1-py2)>46){
        ctx.font="10.5px sans-serif";ctx.fillStyle=MUTED;ctx.textAlign="left";
        ctx.fillText(fmt(ln.y1),px+8,py1);
        ctx.fillText(fmt(ln.y2),px+8,py2);
      }
    }else{
      const x1=dt2x(Date.parse(ln.x1)),x2=dt2x(Date.parse(ln.x2));
      ctx.moveTo(x2px(x1),p2y(ln.y1));
      ctx.lineTo(x2px(x2),p2y(ln.y2));
      ctx.stroke();
    }
    if(sel){
      ctx.setLineDash([]);
      for(const [x,y] of lineEndpoints(ln)){
        const px=x2px(x),py=p2y(y);
        ctx.beginPath();ctx.arc(px,py,5.5,0,Math.PI*2);
        ctx.fillStyle="#fff";ctx.fill();
        ctx.strokeStyle=col;ctx.lineWidth=2;ctx.stroke();
      }
    }
    ctx.restore();
  });
}

function drawPreview(mx,my){
  if(!S.mode)return;
  ctx.save();
  ctx.strokeStyle="rgba(255,255,255,.85)";ctx.lineWidth=1;ctx.setLineDash([5,4]);
  if(S.mode==="trend"&&S.p1){
    const px1=x2px(dt2x(S.p1.t)),py1=p2y(S.p1.y);
    ctx.beginPath();ctx.moveTo(px1,py1);ctx.lineTo(mx,my);ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();ctx.arc(px1,py1,4,0,Math.PI*2);ctx.fillStyle="#fff";ctx.fill();
  }else if(S.mode==="v"&&S.p1){
    const px1=x2px(dt2x(S.p1.t)),py1=p2y(S.p1.y);
    ctx.beginPath();ctx.moveTo(px1,py1);ctx.lineTo(px1,my);ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();ctx.arc(px1,py1,4,0,Math.PI*2);ctx.fillStyle="#fff";ctx.fill();
    const pc=S.p1.y?(y2p(my)/S.p1.y-1)*100:0;
    ctx.font="700 11px sans-serif";ctx.fillStyle=pctColor(pc);
    ctx.textAlign="left";ctx.textBaseline="middle";
    ctx.fillText(fmtPct(pc),px1+9,(py1+my)/2);
  }
  ctx.restore();
}

function drawCrosshair(){
  const h=S.hover;
  if(!h||S.drag||S.pan)return;
  if(h.mx<G.pad.l||h.mx>G.w-G.pad.r)return;
  ctx.save();
  ctx.strokeStyle="rgba(200,215,230,.35)";ctx.lineWidth=1;ctx.setLineDash([3,3]);
  ctx.beginPath();
  ctx.moveTo(Math.round(h.mx)+0.5,G.pad.t);
  ctx.lineTo(Math.round(h.mx)+0.5,G.pad.t+G.mainH);
  ctx.stroke();
  if(h.my>=G.pad.t&&h.my<=G.pad.t+G.mainH){
    ctx.beginPath();
    ctx.moveTo(G.pad.l,Math.round(h.my)+0.5);
    ctx.lineTo(G.w-G.pad.r,Math.round(h.my)+0.5);
    ctx.stroke();
    ctx.setLineDash([]);
    const txt=fmt(y2p(h.my));
    ctx.font="11px sans-serif";
    const tw=ctx.measureText(txt).width+10;
    ctx.fillStyle="#39434f";ctx.fillRect(G.w-G.pad.r+1,h.my-8,Math.max(tw,58),16);
    ctx.fillStyle="#fff";ctx.textAlign="left";ctx.textBaseline="middle";
    ctx.fillText(txt,G.w-G.pad.r+6,h.my);
  }
  ctx.restore();
}

/* ===================== 选择 / 命中 ===================== */
function updateTip(){
  const tip=$('tip');
  const h=S.hover;
  if(!h||S.drag||S.pan||!nBars()||h.mx<G.pad.l||h.mx>G.w-G.pad.r){
    tip.style.display='none';return;
  }
  const idx=Math.max(0,Math.min(nBars()-1,Math.round(px2x(h.mx))));
  const b=S.bars[idx];
  const prev=idx>0?S.bars[idx-1].c:b.o;
  const pc=prev?(b.c/prev-1)*100:0;
  const ds=tickLabelShort(b);
  tip.innerHTML=`<div><b>${ds}</b> 价 <b>${fmt(b.c)}</b> <span style="color:${pctColor(pc)}">${fmtPct(pc)}</span></div>`+
    `<div style="color:${MUTED}">量 ${b.slot===1?fmtVol(b.v):'--'}</div>`;
  tip.style.display='block';
  const tw=tip.offsetWidth,th=tip.offsetHeight;
  let left=h.mx+16,top=h.my+14;
  if(left+tw>G.w-8)left=h.mx-tw-16;
  if(top+th>G.h-8)top=Math.max(4,G.h-th-8);
  tip.style.left=left+'px';
  tip.style.top=top+'px';
}
function distPointSeg(px,py,x1,y1,x2,y2){
  const dx=x2-x1,dy=y2-y1;
  if(dx===0&&dy===0)return Math.hypot(px-x1,py-y1);
  let t=((px-x1)*dx+(py-y1)*dy)/(dx*dx+dy*dy);
  t=Math.max(0,Math.min(1,t));
  return Math.hypot(px-(x1+t*dx),py-(y1+t*dy));
}
function nearestHandle(mx,my){
  if(S.selected===null)return null;
  const arr=curLines(),ln=arr[S.selected];
  if(!ln)return null;
  const pts=lineEndpoints(ln);
  let best=-1,bd=1e18;
  pts.forEach(([x,y],i)=>{
    const px=x2px(x),py=p2y(y);
    const d=Math.hypot(mx-px,my-py);
    if(d<bd){bd=d;best=i;}
  });
  return bd<=12?{i:best,d:bd}:null;
}
function nearestLine(mx,my,onlyType){
  const arr=curLines();
  let best=null,bd=1e18;
  arr.forEach((ln,i)=>{
    if(onlyType&&ln.type!==onlyType)return;
    let d;
    if(ln.type==="v"){
      const px=x2px(dt2x(Date.parse(ln.x))),py1=p2y(ln.y1),py2=p2y(ln.y2);
      const lo=Math.min(py1,py2),hi=Math.max(py1,py2);
      d=(my>=lo&&my<=hi)?Math.abs(mx-px)
        :Math.hypot(mx-px,my-(Math.abs(my-py1)<Math.abs(my-py2)?py1:py2));
      d-=1;
    }else{
      const x1=dt2x(Date.parse(ln.x1)),x2=dt2x(Date.parse(ln.x2));
      d=distPointSeg(mx,my,x2px(x1),p2y(ln.y1),x2px(x2),p2y(ln.y2));
    }
    if(d<bd){bd=d;best=i;}
  });
  return bd<=9?{idx:best,d:bd}:null;
}

/* ===================== 撤销 / 保存 ===================== */
function pushUndo(){
  S.undo.push(JSON.stringify(curLines()));
  if(S.undo.length>200)S.undo.shift();
  S.redo.length=0;
  updateButtons();
}
function applySnapshot(json){
  S.allLines[lineKey()]=JSON.parse(json);
  S.selected=null;
  saveLines();
  draw();
}
function undo(){
  if(!S.undo.length)return setStatus('没有可撤销的操作');
  S.redo.push(JSON.stringify(curLines()));
  applySnapshot(S.undo.pop());
  setStatus('已撤销 (Ctrl+Z)');
  updateButtons();
}
function redo(){
  if(!S.redo.length)return setStatus('没有可重做的操作');
  S.undo.push(JSON.stringify(curLines()));
  applySnapshot(S.redo.pop());
  setStatus('已重做 (Ctrl+Y)');
  updateButtons();
}
let _saveTimer=0;
function saveLines(){
  clearTimeout(_saveTimer);
  _saveTimer=setTimeout(()=>{
    api('/api/lines',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:lineKey(),lines:curLines()})}).catch(()=>{});
  },220);
}
async function loadLines(){
  try{const d=await api('/api/lines');S.allLines=d.lines||{};}
  catch(e){S.allLines={};}
}
function addLine(ln){
  pushUndo();S.selected=null;
  const arr=curLines();
  const limit=ln.type==='trend'?S.lineLimitTrend:S.lineLimitV;
  const same=arr.filter(x=>x.type===ln.type).length;
  if(same>=limit){
    const idx=arr.findIndex(x=>x.type===ln.type);
    if(idx>=0)arr.splice(idx,1);
  }
  arr.push(ln);
  saveLines();draw();updateButtons();
}
function deleteSelected(){
  if(S.selected===null)return setStatus('先点击选中一条线，再按 Delete');
  pushUndo();
  curLines().splice(S.selected,1);
  S.selected=null;
  saveLines();draw();updateButtons();
  setStatus('已删除该线（Ctrl+Z 可撤销）');
}
function clearLines(){
  if(!curLines().length)return setStatus('当前标的没有线');
  pushUndo();
  S.allLines[lineKey()]=[];
  S.selected=null;
  saveLines();draw();updateButtons();
  setStatus('已清空当前标的的线');
}

function setMode(mode){
  S.mode=(S.mode===mode)?null:mode;
  S.p1=null;
  syncModeButtons();
  if(S.mode==="trend")setStatus('趋势线：点第一个点 → 移动预览 → 点第二个点（Esc / 右键取消）');
  else if(S.mode==="v")setStatus('纵向线：点起点 → 点终点（同一时刻上下两价），显示这段涨幅（Esc / 右键取消）');
  else setStatus('已退出画线模式（左键拖拽平移 / 滚轮缩放）');
  draw();
}
function syncModeButtons(){
  $('bT').classList.toggle('on',S.mode==='trend');
  $('bV').classList.toggle('on',S.mode==='v');
}
function updateButtons(){
  $('bUndo').disabled=!S.undo.length;
  $('bRedo').disabled=!S.redo.length;
  $('bDel').disabled=S.selected===null;
}

/* ===================== 绘制 ===================== */
function drawGrid(i0,i1){
  const yl=S.ylim||{lo:0,hi:1};
  ctx.save();
  ctx.strokeStyle="#222a33";ctx.fillStyle=MUTED;
  ctx.lineWidth=1;ctx.font="10px sans-serif";
  ctx.textAlign="left";ctx.textBaseline="middle";
  for(let g=0;g<=5;g++){
    const py=Math.round(G.pad.t+g/5*G.mainH)+0.5;
    ctx.beginPath();ctx.moveTo(G.pad.l,py);ctx.lineTo(G.w-G.pad.r,py);ctx.stroke();
    let val;
    if(S.logScale){
      const lo=Math.log(Math.max(yl.lo,1e-9)),hi=Math.log(Math.max(yl.hi,1e-9));
      val=Math.exp(hi-g/5*(hi-lo));
    }else{
      val=yl.hi-g/5*(yl.hi-yl.lo);
    }
    ctx.fillText(fmtAxis(val),4,py);
  }
  if(i1>i0){
    const perDay=S.unitMode==='half'?2:1;
    const step=Math.max(1,Math.round((i1-i0)/perDay/6));
    const d0=S.bars[i0].day,w0=S.bars[i0].week||0;
    ctx.textAlign="center";ctx.textBaseline="top";
    for(let i=i0;i<=i1;i++){
      const b=S.bars[i];
      if(S.unitMode==='half'&&b.slot!==0)continue;
      const key=S.unitMode==='week'?((b.week||0)-w0):(b.day-d0);
      if(key%step!==0)continue;
      const px=x2px(i);
      if(px<G.pad.l-1||px>G.w-G.pad.r+1)continue;
      const d=new Date(b.t);
      const pad2=v=>String(v).padStart(2,'0');
      ctx.fillText(pad2(d.getUTCMonth()+1)+'-'+pad2(d.getUTCDate()),px,G.volTop+G.volH+6);
    }
  }
  ctx.restore();
}
function drawSeries(i0,i1){
  ctx.save();
  ctx.lineJoin="round";
  ctx.strokeStyle="#6ab8ff";ctx.lineWidth=2.2;
  ctx.shadowColor="rgba(90,175,255,.45)";ctx.shadowBlur=8;
  ctx.beginPath();
  for(let i=i0;i<=i1;i++){
    const px=x2px(i),py=p2y(S.bars[i].c);
    if(i===i0)ctx.moveTo(px,py);else ctx.lineTo(px,py);
  }
  ctx.stroke();ctx.shadowBlur=0;
  // 每段建仓→清仓：赚绿 / 亏红，标注盈亏%
  for(const t of S.roundTrips){
    const a=Math.max(i0,Math.min(t.iOpen,S.reveal));
    const b=Math.min(i1,t.iClose);
    if(b<=a)continue;
    const col=t.win?DOWN:UP;
    ctx.strokeStyle=col;ctx.lineWidth=4;ctx.lineCap='round';
    ctx.beginPath();
    for(let i=a;i<=b;i++){
      const px=x2px(i),py=p2y(S.bars[i].c);
      if(i===a)ctx.moveTo(px,py);else ctx.lineTo(px,py);
    }
    ctx.stroke();
    const mid=Math.floor((t.iOpen+t.iClose)/2);
    if(mid>=i0&&mid<=i1){
      const px=x2px(mid),py=p2y(S.bars[mid].c);
      const lab=fmtPct(t.ret);
      ctx.font="700 11px sans-serif";
      const tw=ctx.measureText(lab).width+8;
      ctx.fillStyle="rgba(14,17,22,.9)";
      ctx.fillRect(px-tw/2,py-22,tw,16);
      ctx.fillStyle=col;ctx.textAlign="center";ctx.textBaseline="middle";
      ctx.fillText(lab,px,py-14);
    }
  }
  // 买卖标记（只画已经回放到的时点，避免打开旧记录时把“未来”交易也画出来）
  for(const tr of S.trades){
    if(tr.i>S.reveal)continue;
    const xx=x2px(tr.i);
    if(xx<G.pad.l||xx>G.w-G.pad.r)continue;
    const col=tr.side==='buy'?UP:DOWN;
    ctx.fillStyle=col;ctx.beginPath();ctx.arc(xx,p2y(tr.price),4,0,7);ctx.fill();
    ctx.fillStyle=col;ctx.font="700 11px sans-serif";ctx.textAlign="center";
    ctx.fillText((tr.side==='buy'?'B':'S')+' '+fmtShares(tr.shares),xx,p2y(tr.price)-7);
  }
  // 当前价虚线 + 标签
  const cp=livePrice();
  if(cp){
    const py=Math.round(p2y(cp))+0.5;
    ctx.setLineDash([3,3]);
    ctx.strokeStyle="rgba(255,255,255,.5)";ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(G.pad.l,py);ctx.lineTo(G.w-G.pad.r,py);ctx.stroke();
    ctx.setLineDash([]);
    const lab=fmt(cp);
    ctx.font="700 11px sans-serif";
    const tw=ctx.measureText(lab).width+10;
    ctx.fillStyle=pctColor(S.reveal>0?(S.bars[S.reveal].c/S.bars[S.reveal-1].c-1)*100:0);
    ctx.fillRect(G.w-G.pad.r+1,py-8,Math.max(tw,58),16);
    ctx.fillStyle="#fff";ctx.textAlign="left";ctx.textBaseline="middle";
    ctx.fillText(lab,G.w-G.pad.r+6,py);
  }
  // 当前日期竖线 + 标签
  const rx=Math.round(x2px(S.reveal))+0.5;
  if(rx>=G.pad.l&&rx<=G.w-G.pad.r){
    ctx.setLineDash([3,3]);
    ctx.strokeStyle="rgba(255,255,255,.4)";ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(rx,G.pad.t);ctx.lineTo(rx,G.pad.t+G.mainH);ctx.stroke();
    ctx.setLineDash([]);
    const ds=tickLabelShort(S.bars[S.reveal]);
    ctx.font="700 11px sans-serif";ctx.textAlign="center";ctx.textBaseline="middle";
    const tw=ctx.measureText(ds).width+12;
    const ty=G.pad.t+10;
    ctx.fillStyle="rgba(14,17,22,.85)";
    ctx.fillRect(rx-tw/2,ty-9,tw,18);
    ctx.fillStyle="#fff";
    ctx.fillText(ds,rx,ty);
  }
  ctx.restore();
}
function drawMAs(i0,i1){
  if(!nBars())return;
  const end=Math.min(i1,S.reveal);
  if(end<i0)return;
  const n=nBars();
  const pre=new Float64Array(n+1);
  for(let i=0;i<n;i++)pre[i+1]=pre[i]+S.bars[i].c;
  ctx.save();
  ctx.lineJoin="round";ctx.lineCap="round";
  for(const d of MA_DEFS){
    if(!S.ma[d.p])continue;
    let started=false;
    ctx.beginPath();
    for(let i=i0;i<=end;i++){
      const idx=i+1;
      if(idx<d.p)continue;
      const ma=(pre[idx]-pre[idx-d.p])/d.p;
      const px=x2px(i),py=p2y(ma);
      if(!started){ctx.moveTo(px,py);started=true;}else ctx.lineTo(px,py);
    }
    if(started){
      ctx.strokeStyle=d.color;ctx.lineWidth=1.3;ctx.globalAlpha=.9;ctx.stroke();
    }
  }
  ctx.globalAlpha=1;
  ctx.restore();
}
function drawMALegend(){
  if(!nBars())return;
  const i=Math.min(S.reveal,nBars()-1);
  let x=G.pad.l+8;
  const y=G.pad.t+11;
  ctx.save();
  ctx.font="10px sans-serif";ctx.textAlign="left";ctx.textBaseline="middle";
  for(const d of MA_DEFS){
    if(!S.ma[d.p]||i<d.p-1)continue;
    let sum=0;for(let k=i-d.p+1;k<=i;k++)sum+=S.bars[k].c;
    const txt=`MA${d.p} ${fmtAxis(sum/d.p)}`;
    ctx.fillStyle=d.color;ctx.fillText(txt,x,y);
    x+=ctx.measureText(txt).width+12;
  }
  ctx.restore();
}
function drawVolume(i0,i1){
  ctx.save();
  let vmax=0;
  for(let i=i0;i<=i1;i++){const v=S.bars[i].v||0;if(v>vmax)vmax=v;}
  if(vmax<=0)vmax=1;
  const vh=G.volH;
  const bottom=G.volTop+vh;
  ctx.strokeStyle="#222a33";ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(G.pad.l,bottom+0.5);ctx.lineTo(G.w-G.pad.r,bottom+0.5);ctx.stroke();
  const bw=plotW()/(S.view.r-S.view.l);
  for(let i=i0;i<=i1;i++){
    const b=S.bars[i];
    if(b.slot!==1)continue;              // 半日模式只在 14:55（收盘）显示成交量
    const px=x2px(i);
    if(px<G.pad.l||px>G.w-G.pad.r)continue;
    const hgt=Math.max(1,((b.v||0)/vmax)*vh);
    const col=(i>0?b.c>=S.bars[i-1].c:true)?UP:DOWN;
    const w=Math.max(1,Math.min(9,bw*1.4));
    ctx.fillStyle=col;ctx.globalAlpha=0.85;
    ctx.fillRect(px-w/2,bottom-hgt,w,hgt);
  }
  ctx.globalAlpha=1;
  ctx.fillStyle=MUTED;ctx.font="10px sans-serif";ctx.textAlign="left";ctx.textBaseline="top";
  ctx.fillText('VOL '+fmtVol(vmax),G.pad.l+2,G.volTop+2);
  ctx.restore();
}
function drawPaneDivider(){
  const y=Math.round(G.volTop-2)+0.5;
  ctx.save();
  ctx.strokeStyle="rgba(140,160,180,.45)";ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(G.pad.l,y);ctx.lineTo(G.w-G.pad.r,y);ctx.stroke();
  const cx=(G.pad.l+G.w-G.pad.r)/2;
  ctx.strokeStyle="rgba(200,215,230,.8)";ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(cx-16,y);ctx.lineTo(cx+16,y);ctx.stroke();
  ctx.restore();
}
function draw(){
  if(!G.w||!G.h)return;
  ctx.clearRect(0,0,G.w,G.h);
  const n=nBars();
  if(!n){
    ctx.fillStyle=MUTED;ctx.font="14px sans-serif";
    ctx.textAlign="center";ctx.textBaseline="middle";
    ctx.fillText('选择左侧标的开始',G.w/2,G.h/2);
    return;
  }
  const [i0,i1]=visibleIdx();
  autoscale(i0,i1);
  drawGrid(i0,i1);
  drawMAs(i0,i1);
  drawSeries(i0,i1);
  drawVolume(i0,i1);
  drawPaneDivider();
  drawLines();
  drawMALegend();
  if(S.hover)drawPreview(S.hover.mx,S.hover.my);
  if(S.hover)drawCrosshair();
  updateTip();
  updateHUD();
  updateVolStats();
  updateVT();
  updateValuation();
  updatePerfStats();
}

/* ===================== 交互 ===================== */
function evPos(e){
  const r=cv.getBoundingClientRect();
  return {mx:e.clientX-r.left,my:e.clientY-r.top};
}
function inMain(my){return my>=G.pad.t&&my<=G.pad.t+G.mainH;}

cv.addEventListener('contextmenu',e=>e.preventDefault());

cv.addEventListener('pointerdown',e=>{
  if(!nBars())return;
  const {mx,my}=evPos(e);
  if(e.button===2){
    if(S.mode){S.mode=null;S.p1=null;syncModeButtons();}
    else{S.selected=null;updateButtons();}
    setStatus('已退出画线 / 取消选中');
    draw();return;
  }
  if(e.button!==0)return;
  // 0) 价格/成交量分隔线：上下拖动调整高度
  if(Math.abs(my-G.volTop)<=7&&mx>=G.pad.l-8&&mx<=G.w-G.pad.r+8){
    S.volDrag={y0:my,vol0:G.volH};
    try{cv.setPointerCapture(e.pointerId);}catch(_){}
    cv.style.cursor='ns-resize';
    return;
  }
  // 1) 端点
  const hd=S.mode?null:nearestHandle(mx,my);
  if(hd){
    pushUndo();
    S.drag={kind:'handle',idx:S.selected,i:hd.i,moved:false};
    cv.setPointerCapture(e.pointerId);
    setStatus('拖动端点调整位置（松开保存）');
    return;
  }
  // 2) 线体（画线模式下不选中任何线）
  const nl=S.mode?null:nearestLine(mx,my,null);
  if(nl){
    S.selected=nl.idx;
    const arr=curLines(),ln=arr[nl.idx];
    pushUndo();
    S.drag={kind:'move',idx:nl.idx,startX:mx,startY:my,moved:false,
      orig:ln.type==='h'?{y:ln.y}
        :ln.type==='v'?{x:dt2x(Date.parse(ln.x)),y1:ln.y1,y2:ln.y2}
        :{x1:dt2x(Date.parse(ln.x1)),y1:ln.y1,x2:dt2x(Date.parse(ln.x2)),y2:ln.y2}};
    cv.setPointerCapture(e.pointerId);
    updateButtons();draw();
    setStatus('已选中第 '+(nl.idx+1)+' 条'+LINE_NAMES[ln.type]+'：拖线体平移，拖两端圆点调整，点空白取消');
    return;
  }
  // 3) 画线
  if(S.mode==='trend'&&inMain(my)){
    const p={t:x2dt(px2x(mx)),y:y2p(my)};
    if(!S.p1){S.p1=p;setStatus('趋势线：移动鼠标预览，点击第二个点完成（右键取消）');draw();}
    else{
      const p1=S.p1;S.p1=null;
      if(Math.abs(dt2x(p.t)-dt2x(p1.t))<1e-9){setStatus('两点时间太近，请重新画');draw();return;}
      addLine({type:'trend',x1:isoFromMs(p1.t),y1:round4(p1.y),x2:isoFromMs(p.t),y2:round4(p.y)});
      S.mode=null;syncModeButtons();
      setStatus('已添加趋势线 · 已退出画线模式');draw();
    }
    return;
  }
  if(S.mode==='v'&&inMain(my)){
    if(!S.p1){S.p1={t:x2dt(px2x(mx)),y:y2p(my)};
      setStatus('纵向线：已定起点，移动鼠标预览涨幅，点第二个点完成（右键取消）');draw();}
    else{
      const p1=S.p1;S.p1=null;
      const y2=y2p(my);
      if(Math.abs(p1.y-y2)<1e-9){setStatus('两点价格太近，请重新画');draw();return;}
      addLine({type:'v',x:isoFromMs(p1.t),y1:round4(p1.y),y2:round4(y2)});
      S.mode=null;syncModeButtons();
      setStatus('已添加纵向线：'+fmtPct((y2/p1.y-1)*100)+' · 已退出画线模式');draw();
    }
    return;
  }
  // 4) 空白 → 取消选中 + 平移
  if(S.selected!==null){S.selected=null;updateButtons();draw();}
  S.pan={mx,my,l:S.view.l,r:S.view.r};
  cv.setPointerCapture(e.pointerId);
});

cv.addEventListener('pointermove',e=>{
  const {mx,my}=evPos(e);
  if(S.volDrag){
    const dy=my-S.volDrag.y0;
    const avail=Math.max(80,G.h-G.pad.t-G.pad.b-4);
    const minMain=Math.min(140,Math.max(60,avail*0.35));
    const maxVol=Math.max(30,avail-minMain);
    let vh=S.volDrag.vol0-dy;
    vh=Math.max(35,Math.min(vh,maxVol));
    G.volH=vh;G.mainH=avail-vh;G.volTop=G.pad.t+G.mainH+4;
    G.volRatio=vh/G.h;
    S.hover=null;
    requestDraw();return;
  }
  if(!S.pan&&!S.drag){
    const onDiv=Math.abs(my-G.volTop)<=7&&mx>=G.pad.l-8&&mx<=G.w-G.pad.r+8;
    cv.style.cursor=onDiv?'ns-resize':'crosshair';
  }
  if(S.pan){
    const dx=(mx-S.pan.mx)/plotW()*(S.pan.r-S.pan.l);
    S.view={l:S.pan.l-dx,r:S.pan.r-dx};
    S.userView=true;
    requestDraw();return;
  }
  if(S.drag){
    const arr=curLines(),ln=arr[S.drag.idx];
    if(!ln)return;
    S.drag.moved=true;
    if(S.drag.kind==='handle'){
      const x=x2dt(px2x(mx)),y=round4(y2p(my));
      if(ln.type==='h')ln.y=y;
      else if(ln.type==='v'){if(S.drag.i===0)ln.y1=y;else ln.y2=y;}
      else if(S.drag.i===0){ln.x1=isoFromMs(x);ln.y1=y;}
      else{ln.x2=isoFromMs(x);ln.y2=y;}
    }else{
      const dx=px2x(mx)-px2x(S.drag.startX);
      const dyPx=my-S.drag.startY;
      const o=S.drag.orig;
      const sh=p=>y2p(p2y(p)+dyPx);
      if(ln.type==='h')ln.y=round4(sh(o.y));
      else if(ln.type==='v'){
        ln.x=isoFromMs(x2dt(o.x+dx));
        ln.y1=round4(sh(o.y1));ln.y2=round4(sh(o.y2));
      }else{
        ln.x1=isoFromMs(x2dt(o.x1+dx));ln.y1=round4(sh(o.y1));
        ln.x2=isoFromMs(x2dt(o.x2+dx));ln.y2=round4(sh(o.y2));
      }
    }
    requestDraw();return;
  }
  S.hover={mx,my};
  requestDraw();
});

cv.addEventListener('pointerup',e=>{
  if(S.volDrag){
    S.volDrag=null;
    cv.style.cursor='crosshair';
    try{localStorage.setItem('btg.volRatio',String(G.volRatio));}catch(_){}
    return;
  }
  if(S.pan){S.pan=null;return;}
  if(S.drag){
    const moved=S.drag.moved;
    S.drag=null;
    if(moved){saveLines();setStatus('已保存线条调整');}
    else{S.undo.pop();updateButtons();}
    draw();
  }
});
cv.addEventListener('pointerleave',()=>{S.hover=null;if(!S.volDrag)cv.style.cursor='crosshair';requestDraw();});

cv.addEventListener('wheel',e=>{
  if(!nBars())return;
  e.preventDefault();
  const {mx}=evPos(e);
  if(mx<G.pad.l||mx>G.w-G.pad.r)return;
  const factor=e.deltaY<0?0.93:1.075;
  const xd=px2x(mx);
  const l0=S.view.l,r0=S.view.r;
  let l=xd-(xd-l0)*factor;
  let r=l+(r0-l0)*factor;
  if(r-l<8)return;
  if(r-l>nBars()*4){const c=(l+r)/2;l=c-nBars()*2;r=c+nBars()*2;}
  S.view={l,r};
  S.userView=true;
  requestDraw();
},{passive:false});

cv.addEventListener('dblclick',()=>{resetView();setStatus('已重置视图');});

let _raf=0,_dirty=false;
function requestDraw(){
  _dirty=true;
  if(_raf)return;
  _raf=requestAnimationFrame(()=>{_raf=0;_dirty=false;draw();});
}

document.addEventListener('keydown',e=>{
  if($('confirmMask').style.display==='flex')return;
  const tag=(e.target.tagName||'').toLowerCase();
  if(tag==='input'||tag==='select'||tag==='textarea'){
    if(e.key==='Escape')e.target.blur();
    return;
  }
  if(e.key==='ArrowLeft'){e.preventDefault();prevStep();return;}
  if(e.key==='ArrowRight'){e.preventDefault();if(S.playing&&!S.timer)step();return;}
  if(e.key==='Escape'){
    if(S.p1){S.p1=null;setStatus('已取消起点');draw();return;}
    if(S.mode){setMode(null);return;}
    if(S.selected!==null){S.selected=null;updateButtons();draw();}
    return;
  }
  if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();deleteSelected();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();if(e.shiftKey)redo();else undo();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='y'){e.preventDefault();redo();}
});

/* ===================== 游戏 ===================== */
function currentPrice(){return S.bars[Math.min(S.reveal,nBars()-1)].c}
function trackEquity(){
  if(!nBars())return;
  const p=currentPrice();
  const eq=S.cash+S.shares*p;
  const bmEq=S.benchPrice?(100000/S.benchPrice)*p:100000;
  if(eq>S.peakEquity)S.peakEquity=eq;
  const dd=(S.peakEquity-eq)/S.peakEquity;
  if(dd>S.maxDD)S.maxDD=dd;
  if(bmEq>S.benchPeak)S.benchPeak=bmEq;
  const bdd=(S.benchPeak-bmEq)/S.benchPeak;
  if(bdd>S.benchMaxDD)S.benchMaxDD=bdd;
}
/* ===================== 步进回退 / 至今表现 ===================== */
function equityNow(){return S.cash+S.shares*currentPrice();}

function resetPerfHistory(){
  S.history=[];
  S.eqHist=[100000];
}

function recordEquity(){
  if(!Array.isArray(S.eqHist))S.eqHist=[];
  const idx=S.reveal-S.startIdx;
  if(idx<0)return;
  S.eqHist[idx]=equityNow();
  S.eqHist.length=idx+1;
}

function updateEquityNow(){
  if(!Array.isArray(S.eqHist))return;
  const idx=S.reveal-S.startIdx;
  if(idx>=0&&idx<S.eqHist.length)S.eqHist[idx]=equityNow();
}

function rebuildEqHist(){
  if(!nBars()){S.eqHist=[100000];return;}
  const byI={};
  for(const t of S.trades){(byI[t.i]=byI[t.i]||[]).push(t);}
  let cash=100000, sh=0;
  const arr=[];
  for(let i=S.startIdx;i<=S.reveal;i++){
    for(const t of (byI[i]||[])){
      if(t.side==='buy'){cash-=t.shares*t.price+(t.fee||0);sh+=t.shares;}
      else{cash+=t.shares*t.price-(t.fee||0);sh-=t.shares;}
    }
    arr.push(cash+sh*S.bars[i].c);
  }
  S.eqHist=arr;
}

function pushStepSnapshot(){
  if(!Array.isArray(S.history))S.history=[];
  S.history.push({
    reveal:S.reveal, cash:S.cash, shares:S.shares,
    trades:S.trades.slice(), roundTrips:S.roundTrips.slice(),
    openTrade:S.openTrade?{...S.openTrade}:null,
    peakEquity:S.peakEquity, maxDD:S.maxDD,
    benchPeak:S.benchPeak, benchMaxDD:S.benchMaxDD,
    log:$('tradeLog').innerHTML,
    eqLen:Array.isArray(S.eqHist)?S.eqHist.length:0,
  });
  if(S.history.length>500)S.history.shift();
}

function prevStep(){
  if(S.viewingSaved)return setStatus('已保存记录为只读');
  if(S.timer)return;
  if(!S.history||!S.history.length)return;
  if(S.reveal<=S.startIdx)return setStatus('已经在起点');
  const h=S.history.pop();
  S.reveal=h.reveal;
  S.cash=h.cash;S.shares=h.shares;
  S.trades=h.trades;S.roundTrips=h.roundTrips;S.openTrade=h.openTrade;
  S.peakEquity=h.peakEquity;S.maxDD=h.maxDD;
  S.benchPeak=h.benchPeak;S.benchMaxDD=h.benchMaxDD;
  if(Array.isArray(S.eqHist))S.eqHist.length=h.eqLen;
  $('tradeLog').innerHTML=h.log||'';
  S.userView=false;
  S.view={l:Math.max(-1,S.reveal-viewWindow()),r:S.reveal};
  updateStats();updateSaveBtn();updateHUD();updatePerfStats();
  updateStepButtons();draw();
  setStatus(`已回退 · ${tickLabel(S.bars[S.reveal])} · 可买入/卖出`);
}

function updateStepButtons(){
  const canPrev=!S.viewingSaved && !S.timer &&
                S.reveal>S.startIdx && S.history && S.history.length>0;
  $('btnPrev').disabled=!canPrev;
  $('btnStep').disabled=!S.playing || !!S.timer || S.reveal>=nBars()-1;
  $('btnPause').disabled=!S.playing;
  $('btnStart').disabled=!!S.playing;
}

function calcPerfStats(){
  const minPts=S.unitMode==='half'?60:(S.unitMode==='day'?30:10);
  if(!Array.isArray(S.eqHist)||S.eqHist.length<minPts)return null;
  const ppy=S.unitMode==='half'?504:(S.unitMode==='day'?252:52);
  const rs=[];
  for(let i=1;i<S.eqHist.length;i++){
    const a=S.eqHist[i-1],b=S.eqHist[i];
    if(a>0&&isFinite(b))rs.push(b/a-1);
  }
  const bs=[];
  for(let i=S.startIdx+1;i<=S.reveal;i++){
    const a=S.bars[i-1].c,b=S.bars[i].c;
    if(a>0)bs.push(b/a-1);
  }
  const stat=arr=>{
    if(arr.length<2)return null;
    const m=arr.reduce((s,x)=>s+x,0)/arr.length;
    const v=arr.reduce((s,x)=>s+(x-m)*(x-m),0)/(arr.length-1);
    const sd=Math.sqrt(v);
    return {sd,vol:sd*Math.sqrt(ppy),sharpe:sd>0?m/sd*Math.sqrt(ppy):0};
  };
  const st=stat(rs),bm=stat(bs);
  if(!st||!bm)return null;
  const eq0=S.eqHist[0],eq1=S.eqHist[S.eqHist.length-1];
  const total=eq0>0?eq1/eq0-1:0;
  const b0=S.benchPrice||S.bars[S.startIdx].c,b1=currentPrice();
  const btotal=b0>0?b1/b0-1:0;
  const retdd=S.maxDD>0?total/S.maxDD:null;
  const bretdd=S.benchMaxDD>0?btotal/S.benchMaxDD:null;
  return {st,bm,total,btotal,retdd,bretdd};
}

function updatePerfStats(){
  const el=$('perfStats'),hv=$('hVol'),hbv=$('hBenchVol');
  const r=calcPerfStats();
  if(!r){
    if(el)el.textContent='样本不足（至少 30 个交易日）';
    if(hv)hv.textContent='--';
    if(hbv)hbv.textContent='--';
    return;
  }
  if(hv)hv.textContent=fmt(r.st.vol*100)+'%';
  if(hbv)hbv.textContent=fmt(r.bm.vol*100)+'%';
  if(!el)return;
  const rr=v=>v==null?'--':fmt(v);
  el.innerHTML=
    `区间收益 <b>${fmtPct(r.total*100)}</b> · 持有 <b>${fmtPct(r.btotal*100)}</b><br>`+
    `夏普 <b>${fmt(r.st.sharpe)}</b> · 持有 <b>${fmt(r.bm.sharpe)}</b><br>`+
    `收益/回撤 <b>${rr(r.retdd)}</b> · 持有 <b>${rr(r.bretdd)}</b>`;
}

function showFull(){
  stop();
  S.viewingSaved=false;
  S.saved=false;
  S.cash=100000;S.shares=0;S.trades=[];
  S.openTrade=null;S.roundTrips=[];
  S.peakEquity=100000;S.maxDD=0;S.benchPeak=100000;S.benchMaxDD=0;
  S.startIdx=+$('start').value||0;
  S.reveal=S.startIdx;
  S.benchPrice=nBars()?S.bars[S.startIdx].c:null;
  resetPerfHistory();
  showTradeId(null);
  resetView();
  $('tradeLog').innerHTML='';updateStats();updateSaveBtn();updateHUD();updateStepButtons();draw();
  setStatus(`就绪 · 点「开始回放」从所选日期${S.unitMode==='half'?' 09:55 ':''}开始`);
}
function start(){
  if(S.playing)return;
  if(!nBars())return setStatus('请先选择标的');
  S.viewingSaved=false;
  S.saved=false;
  S.startIdx=+$('start').value||0;
  S.cash=100000;S.shares=0;S.trades=[];S.benchPrice=null;
  S.openTrade=null;S.roundTrips=[];
  S.peakEquity=100000;S.maxDD=0;S.benchPeak=100000;S.benchMaxDD=0;
  S.reveal=S.startIdx;
  S.benchPrice=S.bars[S.startIdx].c;
  resetPerfHistory();
  showTradeId(null);
  S.view={l:Math.max(-1,S.reveal-viewWindow()),r:S.reveal};
  S.userView=false;
  $('tradeLog').innerHTML='';updateStats();updateSaveBtn();
  S.playing=true;
  if($('autoPlay').checked){
    S.timer=setInterval(step,+$('speed').value);
    setStatus(`自动回放中 · ${tickLabel(S.bars[S.reveal])} · 可买入/卖出`);
  }else{
    setStatus(`手动模式 · ${tickLabel(S.bars[S.reveal])} · 点「下一步」推进，可买入/卖出`);
  }
  updateStepButtons();
  draw();
}
function stop(){
  S.playing=false;if(S.timer)clearInterval(S.timer);S.timer=null;
  updateStepButtons();
}
function step(){
  if(S.reveal>=nBars()-1){stop();setStatus('回放结束');return;}
  pushStepSnapshot();
  S.reveal++;trackEquity();recordEquity();
  if(!S.userView)S.view={l:Math.max(-1,S.reveal-viewWindow()),r:S.reveal};
  updateStepButtons();
  requestDraw();
  if(S.reveal>=nBars()-1){stop();setStatus('回放结束');return;}
  setStatus(`回放中 · ${tickLabel(S.bars[S.reveal])} · 可买入/卖出`);
}
function doBuy(){
  if(S.viewingSaved)return setStatus('已保存记录为只读；点「开始回放」重新玩一局');
  const p=currentPrice(),frac=+$('buySize').value,amt=S.cash*frac;
  if(amt<=0)return;
  const sh=Math.floor(amt/(p*(1+FEE)));if(sh<=0)return;
  const cost=sh*p,fee=cost*FEE;
  S.cash-=cost+fee;
  if(!S.openTrade)S.openTrade={i:S.reveal,cost:0,proceeds:0};
  S.openTrade.cost+=cost+fee;
  S.shares+=sh;
  S.trades.push({i:S.reveal,side:'buy',price:p,shares:sh,fee});
  log(`买入 ${sh}${UNIT()} @ ${fmt(p)} 手续费 ${fmt(fee)}`);
  trackEquity();updateEquityNow();updateSaveBtn();draw();
}
function doSell(){
  if(S.viewingSaved)return setStatus('已保存记录为只读；点「开始回放」重新玩一局');
  const p=currentPrice();if(S.shares<=0)return;
  const frac=+$('buySize').value;
  const sh=Math.round(S.shares*frac);if(sh<=0)return;
  const gross=sh*p,fee=gross*FEE,proceeds=gross-fee;
  S.cash+=proceeds;
  S.shares-=sh;
  S.trades.push({i:S.reveal,side:'sell',price:p,shares:sh,fee});
  if(S.openTrade){
    S.openTrade.proceeds+=proceeds;
    if(S.shares<=0){
      const t=S.openTrade;
      const pnl=t.proceeds-t.cost;
      const ret=t.cost>0?pnl/t.cost*100:0;
      S.roundTrips.push({iOpen:t.i,iClose:S.reveal,cost:t.cost,proceeds:t.proceeds,pnl,ret,win:pnl>0});
      S.openTrade=null;
      log(`卖出 ${sh}${UNIT()} @ ${fmt(p)} 手续费 ${fmt(fee)} 清仓 盈亏 ${pnl>=0?'+':''}${fmt(pnl)} (${ret>=0?'+':''}${fmt(ret)}%)`);
    }else{
      log(`卖出 ${sh}${UNIT()} @ ${fmt(p)} 手续费 ${fmt(fee)}（剩余 ${S.shares}${UNIT()}）`);
    }
  }else{
    log(`卖出 ${sh}${UNIT()} @ ${fmt(p)} 手续费 ${fmt(fee)}`);
  }
  trackEquity();updateEquityNow();updateStats();updateSaveBtn();draw();
}
const UNIT=()=>(S.market==='idx'||S.market==='cmd')?'份':'股';
function log(m){
  const d=$('tradeLog');const e=document.createElement('div');
  e.textContent=`[${tickLabel(S.bars[S.reveal])}] ${m}`;
  d.prepend(e);while(d.children.length>100)d.removeChild(d.lastChild);
}
function updateStats(){
  const el=$('tradeStats'),n=S.roundTrips.length;
  if(!n){el.innerHTML='';return}
  let wins=0,sum=0;
  for(const t of S.roundTrips){if(t.win)wins++;sum+=t.ret}
  el.innerHTML=`<b>交易统计</b>（建仓→清仓）<br>`+
    `次数 ${n} · 胜 ${wins} · 负 ${n-wins} · 胜率 ${fmt(wins/n*100)}%<br>`+
    `平均每笔 ${sum>=0?'+':''}${fmt(sum/n)}%`;
}
function updateSaveBtn(){
  const el=$('btnSave');
  if(S.viewingSaved){el.disabled=true;el.textContent='已保存记录（只读）';el.title='';return;}
  if(S.saved){el.disabled=true;el.textContent='✅ 本局已保存';el.title='为避免重复记录，一局只能保存一次';return;}
  el.disabled=!S.roundTrips.length;
  el.textContent='💾 保存交易过程';
  el.title=S.roundTrips.length?'保存本局交易记录':'先完成一笔「买入→卖出」才能保存';
}
async function saveSession(){
  if(S.viewingSaved)return setStatus('这是已保存的回测记录（只读）；点「开始回放」可重新玩一局');
  if(S.saved)return setStatus('本局已保存，避免重复记录；要再存请重新开始一局');
  if(!S.trades.length)return setStatus('还没有交易，先玩一局再保存');
  if(!S.roundTrips.length)return setStatus('还没有完成一笔「买入→卖出」，不能保存');
  const eq=S.cash+S.shares*currentPrice();
  const d=idx=>new Date(S.bars[idx].t).toISOString().slice(0,10);
  const tm=idx=>unitText(S.bars[idx]);
  const payload={
    market:S.market, kind:S.market==='idx'?S.idxKind:'', unit:S.unitMode, code:S.code, name:S.name,
    start:d(S.startIdx), startTime:tm(S.startIdx),
    end:d(Math.min(S.reveal,nBars()-1)), endTime:tm(Math.min(S.reveal,nBars()-1)),
    trades:S.trades.map(t=>({date:d(t.i),time:tm(t.i),side:t.side,price:t.price,shares:t.shares,fee:round4(t.fee)})),
    roundTrips:S.roundTrips.map(t=>({open:d(t.iOpen),openTime:tm(t.iOpen),close:d(t.iClose),closeTime:tm(t.iClose),pnl:round4(t.pnl),ret:round4(t.ret),win:t.win})),
    finalEquity:round4(eq),
    finalCash:round4(S.cash),
    finalShares:S.shares,
    finalReturn:round4(pct(eq,100000)),
    maxDD:round4(S.maxDD),
  };
  const ok=await askConfirm(
    `将保存「${S.name}」本局 ${S.trades.length} 笔交易（${S.roundTrips.length} 笔完整买卖）。\n保存后本局不能再重复保存。`,
    '保存','取消','保存交易过程');
  if(!ok)return;
  try{
    const r=await api('/api/save_trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(r.ok){
      S.saved=true;updateSaveBtn();
      showTradeId(r.id,'已保存 · 交易 ID');
      setStatus('已保存 · 交易 ID '+r.id+'（累计 '+r.n+' 条）');
      loadBacktested();
    }else setStatus('保存失败：'+(r.error||''));
  }catch(e){setStatus('保存失败：'+e.message)}
}
async function loadBacktested(){
  try{
    const r=await api('/api/trades');
    const recs=r.records||[];
    const d=$('backtestedList');d.innerHTML='';
    if(!recs.length){d.innerHTML='<div class="btempty">暂无已回测记录</div>';return}
    recs.forEach(rec=>{
      const trips=rec.roundTrips||[];
      const wr=trips.length?Math.round(trips.filter(t=>t.win).length/trips.length*100):null;
      const ret=rec.finalReturn!=null?+rec.finalReturn:null;
      const e=document.createElement('div');e.className='btitem';
      const n=document.createElement('span');n.className='n';n.textContent=rec.name||rec.code||'';
      const c=document.createElement('span');c.className='c';c.textContent=rec.code||'';
      const tid=rec.id||('T'+(rec.ts||''));
      const ib=document.createElement('span');ib.className='id';ib.textContent='ID';
      ib.title='复制交易 ID：'+tid;
      ib.onclick=ev=>{ev.stopPropagation();copyText(tid).then(ok=>setStatus(ok?('已复制交易 ID：'+tid):('交易 ID：'+tid)));};
      const rr=document.createElement('span');rr.className='r';
      rr.textContent=ret==null?'--':(ret>=0?'+':'')+ret.toFixed(2)+'%';
      rr.style.color=ret==null?MUTED:(ret>0?UP:(ret<0?DOWN:MUTED));
      const del=document.createElement('span');del.className='del';del.textContent='×';
      del.title='删除这条回测记录';
      del.onclick=ev=>{ev.stopPropagation();deleteRecord(rec);};
      e.appendChild(n);e.appendChild(c);e.appendChild(ib);e.appendChild(rr);e.appendChild(del);
      e.title=`ID ${tid} · ${UNIT_NAME[rec.unit||'half']||'半日'} · 胜率 ${wr==null?'--':wr+'%'} · 最大回撤 ${rec.maxDD!=null?(rec.maxDD*100).toFixed(2)+'%':'--'} · ${rec.date||''}`;
      e.onclick=()=>openSavedRecord(rec);
      d.appendChild(e);
    });
  }catch(e){}
}
async function deleteRecord(rec){
  const label=(rec.name||rec.code||'')+' '+(rec.date||'');
  if(!await askConfirm('删除这条回测记录？\n'+label,'删除','取消','删除记录'))return;
  try{
    const r=await api('/api/delete_trade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:rec.id,ts:rec.ts})});
    if(!r.ok)return setStatus('删除失败：'+(r.error||''));
    setStatus('已删除回测记录：'+label);
    await loadBacktested();
    if(S.viewingSaved)showFull();
  }catch(e){setStatus('删除失败：'+e.message)}
}
function applyLineLimitsUI(){
  $('limitTrend').value=S.lineLimitTrend;
  $('limitV').value=S.lineLimitV;
  $('lineHint').textContent=`趋势线最多${S.lineLimitTrend}条 · 纵向线最多${S.lineLimitV}条（新增自动去掉最旧）`;
}
function readLineLimits(){
  const rawT=parseInt($('limitTrend').value);
  const rawV=parseInt($('limitV').value);
  const t=Number.isFinite(rawT)?Math.max(1,Math.min(100,rawT)):20;
  const v=Number.isFinite(rawV)?Math.max(1,Math.min(100,rawV)):5;
  S.lineLimitTrend=t;S.lineLimitV=v;
  applyLineLimitsUI();
  try{
    localStorage.setItem('btg.limitTrend',String(t));
    localStorage.setItem('btg.limitV',String(v));
  }catch(_){}
}
function applyUnitUI(){
  const sel=$('unitMode');
  if(sel)sel.value=S.unitMode;
  const tips={
    half:'半日：09:55 = 开盘价，14:55 = 收盘价；每个时点都可交易',
    day:'一日：每个交易日按收盘价决策',
    week:'周线：每周最后一个交易日按周收盘价决策',
  };
  if($('unitHint'))$('unitHint').textContent=tips[S.unitMode]||'';
}
function applyMarketUI(m){
  S.market=m;
  const us=m==='us',idx=m==='idx',cmd=m==='cmd';
  $('market').value=m;
  $('boardInput').disabled=us||cmd;
  $('boardLabel').textContent=idx?'分类':(cmd?'分类':'板块');
  $('boardInput').placeholder=idx?'指数分类':(cmd?'商品':'中文/代码');
  $('symbolInput').placeholder=idx?'指数/中文':(us?'ticker/公司名':(cmd?'商品/代码':'股票/ETF/指数'));
  $('codeInput').placeholder=idx?'输入指数名 如 上证指数 / 半导体设备'
    :(us?'输入 ticker/公司名 如 AAPL / 特斯拉'
      :(cmd?'输入商品名 如 黄金 / 原油 / 白银':'输入代码/中文名/指数 如 300750 / 茅台 / 上证指数'));
  if(!idx)S.idxKind='';
}
function setStartOption(idx){
  if(!S.bars.length)return;
  idx=Math.max(0,Math.min(nBars()-1,idx));
  const sel=$('start'),want=String(idx);
  if(![...sel.options].some(o=>o.value===want)){
    const o=document.createElement('option');
    o.value=want;
    o.textContent=new Date(S.bars[idx].t).toISOString().slice(0,10);
    sel.appendChild(o);
  }
  sel.value=want;
}
async function openSavedRecord(rec){
  const m=rec.market||'cn';
  const unit=rec.unit||'half';
  S.unitMode=unit;
  applyUnitUI();
  applyMarketUI(m);
  if(m==='idx'){
    S.idxKind=rec.kind||'broad';
    S.boards=IDX_GROUPS.slice();
    boardCombo.setItems(S.boards);
    const grp=S.idxKind==='broad'?'idx_broad':S.idxKind==='ind'?'idx_industry':'idx_concept';
    S.boardCode=grp;
    const g=S.boards.find(x=>x.code===grp);
    boardCombo.setValue(g?g.name:'');
  }else if(m==='cmd'){
    S.idxKind='';
    S.boards=[];
    boardCombo.setItems([]);
    boardCombo.setValue('商品');
    try{
      const j=await api('/api/cmd_list');
      S.symbols=j||[];
      symbolCombo.setItems(S.symbols);
    }catch(_){S.symbols=[];symbolCombo.setItems([]);}
  }else{
    S.idxKind='';
    S.boards=S.boards.filter(x=>!String(x.code).startsWith('idx_'));
    if(!S.boards.length)boardCombo.setValue('');
  }
  await loadKline(rec.code, rec.name, m==='idx'?(rec.kind||'broad'):'');
  if(!S.bars.length)return;
  const byDate=new Map();
  S.days.forEach((b,i)=>byDate.set(new Date(b.t).toISOString().slice(0,10),i));
  const tickOf=(date,time,defSlot)=>{
    if(S.unitMode!=='half'){
      const i=S.bars.findIndex(b=>new Date(b.t).toISOString().slice(0,10)===date);
      return i>=0?i:null;
    }
    const di=byDate.get(date);
    if(di==null)return null;
    const slot=time==='09:55'?0:(time==='14:55'?1:defSlot);
    return di*2+slot;
  };
  const startTick=tickOf(rec.start,rec.startTime,1);
  const endTick=tickOf(rec.end,rec.endTime,1);
  S.startIdx=Math.max(0,Math.min(nBars()-1,startTick==null?0:startTick));
  S.reveal=Math.max(S.startIdx,Math.min(nBars()-1,endTick==null?(nBars()-1):endTick));
  setStartOption(S.startIdx);
  // 只映射能对上的交易，避免日期对不上时全部堆到第 0 根
  S.trades=(rec.trades||[]).map(t=>{
    const i=tickOf(t.date,t.time,1);
    return i==null?null:{i,side:t.side,price:t.price,shares:t.shares,fee:t.fee};
  }).filter(t=>t&&(t.side==='buy'||t.side==='sell'));
  S.roundTrips=(rec.roundTrips||[]).map(t=>{
    const a=tickOf(t.open,t.openTime,1),b=tickOf(t.close,t.closeTime,1);
    if(a==null||b==null)return null;
    return {iOpen:a,iClose:b,cost:0,proceeds:0,pnl:t.pnl,ret:t.ret,win:!!t.win};
  }).filter(Boolean);
  // 还原成“已完成的回测”状态：停在结束日，资金显示最终权益
  S.cash=rec.finalCash!=null?Number(rec.finalCash):(Number(rec.finalEquity)||100000);
  S.shares=Number(rec.finalShares)||0;
  S.openTrade=null;
  S.peakEquity=Number(rec.finalEquity)||S.cash;
  S.maxDD=Math.max(0,Number(rec.maxDD)||0);
  S.benchPrice=S.bars[S.startIdx].c;
  S.benchPeak=100000;S.benchMaxDD=0;
  if(S.benchPrice){
    let peak=100000;
    for(let i=S.startIdx;i<=S.reveal;i++){
      const eq=100000/S.benchPrice*S.bars[i].c;
      if(eq>peak)peak=eq;
      const dd=(peak-eq)/peak;
      if(dd>S.benchMaxDD)S.benchMaxDD=dd;
    }
  }
  S.history=[];
  rebuildEqHist();
  S.userView=false;
  S.viewingSaved=true;
  S.saved=true;
  resetView();
  stop();
  const d=$('tradeLog');d.innerHTML='';
  const u=(m==='idx'||m==='cmd')?'份':'股';
  (rec.trades||[]).forEach(t=>{
    const e=document.createElement('div');
    const time=t.time||(unit==='half'?'14:55':(unit==='week'?'周收盘':'收盘'));
    e.textContent=`[${t.date} ${time}] ${t.side==='buy'?'买入':'卖出'} ${t.shares}${u} @ ${fmt(t.price)} 手续费 ${fmt(t.fee)}`;
    d.appendChild(e);
  });
  updateStats();updateSaveBtn();updateHUD();draw();
  const ret=rec.finalReturn!=null?+rec.finalReturn:null;
  showTradeId(rec.id||('T'+(rec.ts||'')),'交易 ID');
  const tidTxt=rec.id?` · ID ${rec.id}`:'';
  setStatus(`已回测记录（只读）：${rec.name||rec.code} · ${UNIT_NAME[unit]||''} · ${rec.start||''} ~ ${rec.end||''}`+
    ` · 最终收益 ${ret==null?'--':(ret>=0?'+':'')+ret.toFixed(2)+'%'}`+
    ` · 最大回撤 ${rec.maxDD!=null?(rec.maxDD*100).toFixed(2)+'%':'--'}`+
    ` · 点「开始回放」可重播`+tidTxt);
}
function periodsPerYear(){
  const days=(S.market==='us'||S.market==='cmd')?252:242;
  if(S.unitMode==='half')return days*2;
  if(S.unitMode==='week')return 52;
  return days;
}
function calcVolStats(){
  if(!nBars())return null;
  const end=Math.min(S.reveal,nBars()-1);
  let hv=null;
  const first=Math.max(1,end-19);
  const rets=[];
  for(let i=first;i<=end;i++){
    const a=S.bars[i-1].c,b=S.bars[i].c;
    if(a>0&&b>0)rets.push(Math.log(b/a));
  }
  if(rets.length>=2){
    const mean=rets.reduce((x,y)=>x+y,0)/rets.length;
    const varr=rets.reduce((x,y)=>x+(y-mean)*(y-mean),0)/(rets.length-1);
    hv=Math.sqrt(varr)*Math.sqrt(periodsPerYear())*100;
  }
  let atr=null,atrPct=null,atrN=0;
  if(S.days.length){
    const endDay=Math.min(S.days.length-1,S.bars[end].day);
    const startDay=Math.max(0,endDay-19);
    let trSum=0;
    for(let i=startDay;i<=endDay;i++){
      const b=S.days[i],prev=i>0?S.days[i-1].c:b.c;
      const tr=Math.max(b.h-b.l,Math.abs(b.h-prev),Math.abs(b.l-prev));
      trSum+=tr;atrN++;
    }
    if(atrN){
      atr=trSum/atrN;
      const p=S.bars[end].c;
      if(p)atrPct=atr/p*100;
    }
  }
  return {hv,atr,atrPct,rets:rets.length,atrN};
}
/* ===================== 估值（PE/PB/PS + 历史分位） ===================== */
function valClass(pct,v){
  if(v==null||v<=0||pct==null)return 'vn';
  return pct<30?'vg':(pct<=70?'vy':'vr');
}
function valRowHTML(k,v,pct,lossText){
  const cls=valClass(pct,v);
  const vv=(v==null)?'—':((v<=0)?(lossText||'—'):fmt(v));
  const pp=(pct==null)?'—':Math.round(pct)+'%';
  return `<div class="vrow"><span class="k">${k}</span><span class="v">${vv}</span>`+
         `<span class="vbadge ${cls}">${pp}</span></div>`;
}
function barDayIndex(i){
  const b=S.bars[i];
  if(!b)return i;
  return (b.day!=null)?b.day:i;
}
function updateValuation(){
  const el=$('valStats');
  if(!el)return;
  if(!S.val||!S.val.byDate){
    el.innerHTML=`<span style="color:var(--dim)">${esc((S.val&&S.val.reason)||'--')}</span>`;
    return;
  }
  if(!nBars()){el.textContent='--';return;}
  const i=Math.min(S.reveal,nBars()-1);
  const d=S.days[barDayIndex(i)];
  if(!d){el.textContent='--';return;}
  const r=S.val.byDate.get(new Date(d.t).toISOString().slice(0,10));
  if(!r){el.innerHTML='<span style="color:var(--dim)">当日无估值</span>';return;}
  el.innerHTML=valRowHTML('PE',r[1],r[4],'亏损')+
               valRowHTML('PB',r[2],r[5])+
               valRowHTML('PS',r[3],r[6]);
}
async function loadValuation(code){
  if(S.market==='cmd'||S.market==='idx'){
    S.val={rows:[],byDate:null,code,
      reason:S.market==='cmd'?'商品无 PE/PB/PS（估值仅适用于股票）'
                            :'指数无 PE/PB/PS（需用成分股加权）'};
    updateValuation();return;
  }
  S.val={rows:[],byDate:null,reason:'加载中…',code};
  updateValuation();
  try{
    const j=await api('/api/valuation?code='+encodeURIComponent(code));
    if(S.code!==code)return;
    if(!j.rows||!j.rows.length){
      S.val={rows:[],byDate:null,reason:j.reason||'无估值数据',code};
      updateValuation();return;
    }
    const m=new Map();
    for(const r of j.rows)m.set(r[0],r);
    S.val={rows:j.rows,byDate:m,reason:'',code};
    updateValuation();
  }catch(e){
    if(S.code!==code)return;
    S.val={rows:[],byDate:null,reason:'估值加载失败',code};
    updateValuation();
  }
}
/* ===================== 仓位参考：波动率目标 ===================== */
function calcVT(){
  if(!S.days.length)return null;
  const end=Math.min(barDayIndex(S.reveal),S.days.length-1);
  const lb=S.vt.lb;
  if(end<lb)return null;
  const seg=S.days.slice(end-lb,end+1);
  const rets=[];
  for(let i=1;i<seg.length;i++){
    const a=seg[i-1].c,b=seg[i].c;
    if(a>0&&b>0)rets.push(b/a-1);
  }
  if(rets.length<Math.floor(lb*0.8))return null;
  const mean=rets.reduce((s,x)=>s+x,0)/rets.length;
  const varr=rets.reduce((s,x)=>s+(x-mean)*(x-mean),0)/(rets.length-1);
  const vol=Math.sqrt(varr)*Math.sqrt(252);
  if(!isFinite(vol)||vol<=0)return null;
  return {vol,w:Math.min(1,S.vt.target/vol),n:rets.length};
}
function updateVT(){
  const el=$('vtStats');
  if(!el)return;
  if(!nBars()){el.textContent='--';return;}
  const r=calcVT();
  if(!r){el.innerHTML='<span style="color:var(--dim)">历史不足，无法估计波动率</span>';return;}
  const p=currentPrice();
  const eq=S.cash+S.shares*p;
  const cur=eq>0?(S.shares*p/eq):0;
  const gap=r.w-cur;
  const near=Math.abs(gap)<0.05;
  const col=near?MUTED:(gap>0?'#f2c94c':'#ff8b6b');
  el.innerHTML=
    `年化波动(${r.n}日) <b>${fmt(r.vol*100)}%</b><br>`+
    `建议仓位 <b>${Math.round(r.w*100)}%</b> · 当前 <b>${Math.round(cur*100)}%</b><br>`+
    `<span style="color:${col}">${near?'接近建议':'偏离 '+(gap>=0?'+':'')+Math.round(gap*100)+'%'}</span>`;
}
function updateVolStats(){
  const el=$('volStats');
  if(!el)return;
  if(!nBars()){el.textContent='--';return;}
  const s=calcVolStats();
  if(!s){el.textContent='--';return;}
  el.innerHTML=`年化波动率(${s.rets}) <b>${s.hv==null?'--':fmt(s.hv)+'%'}</b><br>`+
    `ATR(${s.atrN}日) <b>${s.atr==null?'--':fmtAxis(s.atr)+' ('+(s.atrPct==null?'--':fmt(s.atrPct)+'%')+')'}</b>`;
}
function updateHUD(){
  if(!nBars())return;
  const p=currentPrice();
  $('hDate').textContent=tickLabel(S.bars[Math.min(S.reveal,nBars()-1)]);
  const eq=S.cash+S.shares*p;
  const bmEq=S.benchPrice?(100000/S.benchPrice)*p:100000;
  $('hEquity').textContent=fmt(eq);
  $('hBench').textContent=fmt(bmEq);
  const r=pct(eq,100000),br=pct(bmEq,100000);
  $('hRet').textContent=(r>=0?'+':'')+fmt(r)+'%';
  $('hRet').className=r>=0?'pos':'neg';
  $('hBenchRet').textContent=(br>=0?'+':'')+fmt(br)+'%';
  $('hBenchRet').className=br>=0?'pos':'neg';
  $('hDD').textContent='-'+fmt(S.maxDD*100)+'%';
  $('hBenchDD').textContent='-'+fmt(S.benchMaxDD*100)+'%';
  $('hPrice').textContent=fmt(p);$('hPos').textContent=S.shares;
}

/* ===================== 网页内确认框（不受 Firefox 弹窗拦截影响） ===================== */
let confirmResolve=null;
function askConfirm(msg,okText='确定',cancelText='取消',title='请确认'){
  if(confirmResolve){const r=confirmResolve;confirmResolve=null;r(false);}
  return new Promise(res=>{
    confirmResolve=res;
    $('confirmTitle').textContent=title;
    $('confirmText').textContent=msg;
    $('confirmOk').textContent=okText;
    $('confirmCancel').textContent=cancelText;
    $('confirmMask').style.display='flex';
    setTimeout(()=>$('confirmOk').focus(),0);
  });
}
function closeConfirm(v){
  if(!confirmResolve)return;
  const r=confirmResolve;confirmResolve=null;
  $('confirmMask').style.display='none';
  r(v);
}
$('confirmOk').onclick=()=>closeConfirm(true);
$('confirmCancel').onclick=()=>closeConfirm(false);
$('confirmMask').addEventListener('click',e=>{if(e.target===$('confirmMask'))closeConfirm(false);});
document.addEventListener('keydown',e=>{
  if($('confirmMask').style.display!=='flex')return;
  if(e.key==='Escape'){e.preventDefault();closeConfirm(false);}
});

/* ===================== 事件绑定 ===================== */
$('btnBuy').onclick=doBuy;$('btnSell').onclick=doSell;
$('btnStart').onclick=async ()=>{
  if(S.playing)return;
  const ok=await askConfirm('将从所选开始日期重新开始，当前回放进度和未保存的交易会清空。','开始回放','取消','开始回放');
  if(ok)start();
};
$('btnPause').onclick=async ()=>{
  if(!S.playing)return;
  const wasAuto=!!S.timer;
  if(S.timer){clearInterval(S.timer);S.timer=null;}
  const ok=await askConfirm('已产生的交易会保留，需要保存请点「保存交易过程」。','停止回放','取消','停止回放');
  if(ok)stop();
  else if(wasAuto&&S.playing&&$('autoPlay').checked){S.timer=setInterval(step,+$('speed').value);updateStepButtons();}
};
$('btnStep').onclick=step;
$('btnPrev').onclick=prevStep;
$('autoPlay').onchange=()=>{
  if(!S.playing)return;
  if($('autoPlay').checked){
    if(!S.timer){S.timer=setInterval(step,+$('speed').value);setStatus(`自动回放中 · ${tickLabel(S.bars[S.reveal])} · 可买入/卖出`);}
  }else{
    if(S.timer){clearInterval(S.timer);S.timer=null;}
    setStatus(`手动模式 · ${tickLabel(S.bars[S.reveal])} · 点「下一步」推进`);
  }
  updateStepButtons();
};
$('btnReset').onclick=showFull;
const loadFromInput=()=>{
  const c=$('codeInput').value.trim();
  if(!c)return;
  if(S.market==='idx')resolveIndex(c);
  else if(S.market==='us')resolveUS(c);
  else if(S.market==='cmd')resolveCmd(c);
  else if(S.market==='cn'&&/[\u4e00-\u9fa5]/.test(c))resolveSymbol(c);
  else loadKline(c,'');
};
$('btnLoad').onclick=loadFromInput;
$('codeInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();loadFromInput();}});
$('btnSave').onclick=saveSession;
$('bT').onclick=()=>setMode('trend');
$('bV').onclick=()=>setMode('v');
$('bUndo').onclick=undo;$('bRedo').onclick=redo;
$('bDel').onclick=deleteSelected;$('bClr').onclick=clearLines;
$('limitTrend').onchange=readLineLimits;
$('limitV').onchange=readLineLimits;
MA_DEFS.forEach(d=>{
  const el=$('ma'+d.p);
  if(!el)return;
  el.checked=!!S.ma[d.p];
  el.onchange=()=>{S.ma[d.p]=el.checked;draw();};
});
$('logScale').onchange=()=>{
  S.logScale=$('logScale').checked;
  try{localStorage.setItem('btg.logScale',S.logScale?'1':'0');}catch(_){}
  draw();
};
const readVTSetting=()=>{
  S.vt.target=parseFloat($('vtTarget').value)||0.20;
  S.vt.lb=parseInt($('vtLB').value)||80;
  try{
    localStorage.setItem('btg.vtTarget',String(S.vt.target));
    localStorage.setItem('btg.vtLB',String(S.vt.lb));
  }catch(_){}
  updateVT();
};
$('vtTarget').onchange=readVTSetting;
$('vtLB').onchange=readVTSetting;
$('market').onchange=async ()=>{
  const m=$('market').value;
  applyMarketUI(m);
  if(m==='us'){
    S.symbolCode='';symbolCombo.setValue('');
    try{
      const j=await api('/api/us_list');
      S.symbols=j||[];
      symbolCombo.setItems(S.symbols);
      if(S.symbols.length)selectSymbol(S.symbols[0].code,S.symbols[0].name);
    }catch(e){S.symbols=[];symbolCombo.setItems([]);}
    setStatus('美股：输入 ticker / 英文公司名 / 中文名搜索');
  }else if(m==='idx'){
    setStatus('指数：选分类，或直接搜索 上证指数 / 深成指 / 通信设备');
    await loadIdxBoards();
  }else if(m==='cmd'){
    setStatus('商品：选标的，或直接搜索 黄金 / 白银 / 原油 / 铜');
    await loadCmdList();
  }else{
    setStatus('A股：选板块/标的，或直接搜索 股票 / ETF / 指数（如 上证）');
    await loadBoards();
  }
};

$('start').onchange=showFull;
$('unitMode').onchange=async ()=>{
  const m=$('unitMode').value;
  if(m===S.unitMode)return;
  S.unitMode=m;
  applyUnitUI();
  if(!S.days.length)return;
  S.viewingSaved=false;
  stop();
  S.bars=buildTicks(S.days,m);
  S.tf=tfForUnit(m);
  S.selected=null;S.mode=null;S.p1=null;S.undo=[];S.redo=[];
  syncModeButtons();
  fillStartDates();
  showFull();
  await loadLines();
  updateButtons();
  draw();
  setStatus(`已切换为${UNIT_NAME[m]}模式 · 点「开始回放」`);
};

window.addEventListener('resize',resize);
if(window.ResizeObserver)new ResizeObserver(()=>resize()).observe($('chartbox'));

/* ===================== 启动 ===================== */
(async function init(){
  try{
    const r=parseFloat(localStorage.getItem('btg.volRatio'));
    if(r>=0.06&&r<=0.5)G.volRatio=r;
    S.logScale=localStorage.getItem('btg.logScale')==='1';
    const lt=parseInt(localStorage.getItem('btg.limitTrend'));
    const lv=parseInt(localStorage.getItem('btg.limitV'));
    if(lt>=1&&lt<=100)S.lineLimitTrend=lt;
    if(lv>=1&&lv<=100)S.lineLimitV=lv;
    const vt=parseFloat(localStorage.getItem('btg.vtTarget'));
    if(vt>=0.1&&vt<=0.5)S.vt.target=vt;
    const vlb=parseInt(localStorage.getItem('btg.vtLB'));
    if([60,80,120].includes(vlb))S.vt.lb=vlb;
  }catch(_){}
  $('logScale').checked=S.logScale;
  $('vtTarget').value=String(S.vt.target);
  $('vtLB').value=String(S.vt.lb);
  applyLineLimitsUI();
  resize();
  await loadLines();
  await loadBoards();
  loadBacktested();
})();
</script>
</body>
</html>
"""


def _cn_prefix(code: str) -> str:
    code = str(code)
    if code.startswith(("60", "68", "90", "50", "51", "52", "56", "58")):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def _looks_etf(code: str) -> bool:
    code = str(code)
    return code.startswith(("50", "51", "52", "56", "58", "15", "16", "18"))


def _cn_etf_list() -> list:
    """场内 ETF/LOF 代码、名称（新浪，缓存）；用于中文搜索和 ETF 名称解析。"""
    global _CN_ETF
    if _CN_ETF is not None:
        return _CN_ETF
    with _ETF_LOCK:
        if _CN_ETF is not None:
            return _CN_ETF
        out, seen = [], set()
        try:
            import akshare as ak
            df = ak.fund_etf_category_sina(symbol="ETF基金")
            for _, r in df.iterrows():
                raw = str(r.get("代码") or "").strip()
                name = str(r.get("名称") or "").strip()
                if not raw or raw.lower() == "nan" or not name or name.lower() == "nan":
                    continue
                code = raw[-6:] if raw[-6:].isdigit() else raw
                if code in seen:
                    continue
                seen.add(code)
                sym = raw if raw[:2] in ("sh", "sz") else _cn_prefix(code) + code
                out.append({"code": code, "name": name, "symbol": sym, "kind": "etf"})
        except Exception:
            out = []
        _CN_ETF = out
    return _CN_ETF


def _etf_split_adjust(bars: list) -> list:
    """新浪 ETF 原始价 -> 拆分/份额折算前复权（处理类似 2025-12-02 的 2:1 折算）。"""
    out = [list(row) for row in bars]
    n = len(out)
    for i in range(n - 1, 0, -1):
        prev_close = out[i - 1][4]
        open_i = out[i][1]
        if prev_close <= 0 or open_i <= 0:
            continue
        ratio = open_i / prev_close
        # ETF 日内涨跌幅有限，超过 25% 的跳空基本是拆分/份额折算
        if 0.75 <= ratio <= 1.33:
            continue
        for j in range(i):
            row = out[j]
            row[1] = round(row[1] * ratio, 3)
            row[2] = round(row[2] * ratio, 3)
            row[3] = round(row[3] * ratio, 3)
            row[4] = round(row[4] * ratio, 3)
            if ratio:
                row[5] = row[5] / ratio
    return out


def _etf_from_tencent(code: str) -> list:
    """腾讯前复权 ETF 日K（腾讯成交量单位是手，这里换成股）。"""
    try:
        df = get_kline(code, days=1600)
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            d = r["date"]
            ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            out.append([ds, round(_fnum(r["open"]), 3), round(_fnum(r["high"]), 3),
                        round(_fnum(r["low"]), 3), round(_fnum(r["close"]), 3),
                        _fnum(r["volume"]) * 100])
        return out
    except Exception:
        return []


def _etf_merge(sina: list, tencent: list) -> list:
    """把新浪老数据按腾讯前复权的首日比例对齐，重叠之后用腾讯前复权。"""
    if not sina:
        return tencent
    if not tencent:
        return sina
    smap = {row[0]: row for row in sina}
    first = None
    for i, row in enumerate(tencent):
        if row[0] in smap:
            first = i
            break
    if first is None:
        return sina
    factor = 1.0
    base = smap[tencent[first][0]][4]
    if base:
        factor = tencent[first][4] / base
    if not factor or factor <= 0 or factor != factor:
        factor = 1.0
    cut = tencent[first][0]
    out = []
    for row in sina:
        if row[0] >= cut:
            break
        out.append([row[0], round(row[1] * factor, 3), round(row[2] * factor, 3),
                    round(row[3] * factor, 3), round(row[4] * factor, 3),
                    row[5] / factor if factor else row[5]])
    out.extend(tencent[first:])
    return out


def _bars_etf(code: str) -> list:
    """场内 ETF 日K：新浪全历史 + 腾讯前复权拼接，处理拆分/份额折算。"""
    code = str(code)
    sym = _cn_prefix(code) + code
    for it in _cn_etf_list():
        if it["code"] == code:
            sym = it["symbol"]
            break
    sina = []
    try:
        import akshare as ak
        df = ak.fund_etf_hist_sina(symbol=sym)
        for _, r in df.iterrows():
            d = r["date"]
            ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            sina.append([ds, round(_fnum(r["open"]), 3), round(_fnum(r["high"]), 3),
                         round(_fnum(r["low"]), 3), round(_fnum(r["close"]), 3),
                         _fnum(r["volume"])])
    except Exception:
        sina = []
    sina = _etf_split_adjust(sina)
    tencent = _etf_from_tencent(code)
    return _etf_merge(sina, tencent)


def _cn_name(code: str) -> str:
    """代码 -> 中文名称：优先腾讯实时行情，失败再用 ETF/股票缓存。"""
    code = str(code)
    try:
        from core.datasets.cn_data import get_quote
        q = get_quote(code)
        if q and q.get("name"):
            return str(q["name"]).strip()
    except Exception:
        pass
    for it in _cn_etf_list():
        if it["code"] == code:
            return it["name"]
    if _CN_UNIVERSE:
        for it in _CN_UNIVERSE:
            if it["code"] == code:
                return it["name"]
    return ""


def _bars_cn(code: str):
    if code in _CN_CACHE:
        return _CN_CACHE[code]
    bars = []
    # ETF/LOF 先走新浪基金接口（股票日线接口不支持场内基金）
    if _looks_etf(code):
        bars = _bars_etf(code)
    if not bars:
        try:
            import akshare as ak
            df = ak.stock_zh_a_daily(symbol=_cn_prefix(code) + code, start_date="20200101",
                                     end_date="20261023", adjust="qfq")
            if df is not None and len(df):
                for _, r in df.iterrows():
                    d = r["date"]
                    ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                    bars.append([ds, round(float(r["open"]), 3), round(float(r["high"]), 3),
                                 round(float(r["low"]), 3), round(float(r["close"]), 3),
                                 float(r["volume"])])
        except Exception:
            bars = []
    if not bars:
        bars = _bars_etf(code)
    if not bars:
        df = get_kline(code, days=1600)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                bars.append([r["date"].strftime("%Y-%m-%d"), round(float(r["open"]), 3),
                             round(float(r["high"]), 3), round(float(r["low"]), 3),
                             round(float(r["close"]), 3), float(r["volume"])])
    _CN_CACHE[code] = bars
    return bars


def _bars_us(code: str):
    df = fetch_daily_kline(code.upper(), days=1600)
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append([str(r["date"].date()), round(float(r["open"]), 3),
                    round(float(r["high"]), 3), round(float(r["low"]), 3),
                    round(float(r["close"]), 3), float(r["volume"])])
    return out


def _yahoo_us_search(q: str) -> list:
    """Yahoo Finance 搜索（ticker / 英文公司名）。"""
    q = str(q or "").strip()
    if not q or not q.isascii():
        return []
    key = q.lower()
    if key in _US_SEARCH_CACHE:
        return _US_SEARCH_CACHE[key]
    out, seen = [], set()
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 30, "newsCount": 0, "enableFuzzyQuery": "false"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"},
            timeout=10,
        )
        data = r.json() if r.ok else {}
        for x in data.get("quotes", []):
            sym = str(x.get("symbol") or "").strip()
            name = str(x.get("shortname") or x.get("longname") or "").strip()
            qt = str(x.get("quoteType") or "").upper()
            if not sym or not name or sym in seen:
                continue
            if qt not in ("EQUITY", "ETF"):
                continue
            if any(c in sym for c in (".", "=", "^")):
                continue
            seen.add(sym)
            out.append({"code": sym, "name": name, "kind": "us"})
            if len(out) >= 30:
                break
    except Exception:
        out = []
    if out:
        _US_SEARCH_CACHE[key] = out
    return out


def _us_name(code: str) -> str:
    code = str(code or "").upper()
    if code in _US_NAME_CACHE:
        return _US_NAME_CACHE[code]
    for it in _yahoo_us_search(code):
        if it["code"].upper() == code:
            _US_NAME_CACHE[code] = it["name"]
            return it["name"]
    return code


def _us_list() -> list:
    return list(_US_POPULAR)


def _search_us(kw: str, limit: int = 30) -> list:
    q = str(kw or "").strip()
    if not q:
        return []
    ql = q.lower()
    out, seen = [], set()
    # 常用中文别名
    for cn, sym in _US_ALIAS.items():
        if q == cn or q in cn or cn in q:
            if sym not in seen:
                seen.add(sym)
                out.append({"code": sym, "name": _us_name(sym), "kind": "us"})
    # 内置热门列表（Yahoo 限流时也能搜到常用标的）
    for it in _US_POPULAR:
        if ql in it["code"].lower() or ql in it["name"].lower():
            if it["code"] not in seen:
                seen.add(it["code"])
                out.append(dict(it, kind="us"))
    # Yahoo（英文 / ticker）
    for it in _yahoo_us_search(q):
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        out.append(it)
        if len(out) >= limit:
            break
    return out[:limit]


def _norm_cn_name(name) -> str:
    return str(name).replace(" ", "").replace("\u3000", "").replace("Ａ", "A").strip()


def _cn_stock_universe() -> list:
    """全部 A 股代码/名称（缓存）；用于中文名模糊搜索。"""
    global _CN_UNIVERSE
    if _CN_UNIVERSE is not None:
        return _CN_UNIVERSE
    with _UNIVERSE_LOCK:
        if _CN_UNIVERSE is not None:
            return _CN_UNIVERSE
        rows = []
        try:
            import akshare as ak
            df = None
            try:
                df = ak.stock_info_a_code_name()
            except Exception:
                df = None
            if df is not None and len(df):
                for _, r in df.iterrows():
                    rows.append((r.get("code"), r.get("name")))
            else:
                specs = [
                    ("stock_info_sh_name_code", "证券代码", "证券简称"),
                    ("stock_info_sz_name_code", "A股代码", "A股简称"),
                    ("stock_info_bj_name_code", "证券代码", "证券简称"),
                ]
                for fn, code_col, name_col in specs:
                    try:
                        d = getattr(ak, fn)()
                        for _, r in d.iterrows():
                            rows.append((r.get(code_col), r.get(name_col)))
                    except Exception:
                        continue
        except Exception:
            rows = []
        out, seen = [], set()
        for code, name in rows:
            c = str(code).strip().split(".")[0]
            if not c or c.lower() == "nan":
                continue
            c = c.zfill(6)
            nm = _norm_cn_name(name)
            if not nm or nm.lower() == "nan" or c in seen:
                continue
            seen.add(c)
            out.append({"code": c, "name": nm})
        if not out:  # 网络不可用时用兜底板块里的股票
            for b in FALLBACK_BOARDS:
                for c, n in b["stocks"]:
                    if c not in seen:
                        seen.add(c)
                        out.append({"code": c, "name": n})
        _CN_UNIVERSE = out
    return _CN_UNIVERSE


def _search_broad(kw: str, limit: int = 12) -> list:
    """宽基指数搜索：供 A 股搜索框直接搜「上证 / 深成指 / 创业板」等。"""
    q = str(kw or "").strip()
    if not q:
        return []
    ql = q.lower()
    targets = []
    target = INDEX_ALIASES.get(q)
    if target:
        targets = [it for it in INDEX_BROAD if it["name"] == target]
    buckets = [[], [], []]
    for it in INDEX_BROAD:
        n = it["name"].lower()
        c = it["code"].lower()
        if n == ql or c == ql:
            buckets[0].append(it)
        elif n.startswith(ql) or c.startswith(ql):
            buckets[1].append(it)
        elif ql in n or ql in c:
            buckets[2].append(it)
    out, seen = [], set()
    for bucket in [targets] + buckets:
        for it in bucket:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            out.append({"code": it["code"], "name": it["name"], "kind": "index",
                        "idxKind": "broad", "market": "idx"})
            if len(out) >= limit:
                return out
    return out


def _search_cn(kw: str, limit: int = 30) -> list:
    kw = str(kw or "").strip()
    if not kw:
        return []
    q = kw.lower()
    items = list(_cn_stock_universe())
    # 股票池不含 ETF/LOF，这里合并场内基金（如 561980 半导体设备ETF）
    seen = {it["code"] for it in items}
    for it in _cn_etf_list():
        if it["code"] not in seen:
            seen.add(it["code"])
            items.append(it)
    buckets = [[], [], []]
    for it in items:
        n = it["name"].lower()
        c = it["code"].lower()
        if n == q or c == q:
            buckets[0].append(it)
        elif n.startswith(q) or c.startswith(q):
            buckets[1].append(it)
        elif q in n or q in c:
            buckets[2].append(it)
    out = []
    for bucket in buckets:
        for it in bucket:
            out.append(it)
            if len(out) >= limit:
                return out
    return out


def _ths_index_list(group: str) -> list:
    """同花顺行业/概念指数列表（缓存）。group: industry / concept"""
    if group in _IDX_LIST_CACHE:
        return _IDX_LIST_CACHE[group]
    with _IDX_LOCK:
        if group in _IDX_LIST_CACHE:
            return _IDX_LIST_CACHE[group]
        kind = "ind" if group == "industry" else "con"
        out = []
        try:
            import akshare as ak
            fn = ak.stock_board_industry_name_ths if group == "industry" else ak.stock_board_concept_name_ths
            df = fn()
            for _, r in df.iterrows():
                name = str(r.get("name") or "").strip()
                code = str(r.get("code") or "").strip()
                if name and code and name.lower() != "nan":
                    out.append({"code": code, "name": name, "kind": kind, "group": group})
        except Exception:
            out = []
        if not out:
            out = [dict(x, kind=kind, group=group) for x in _INDEX_FALLBACK.get(group, [])]
        _IDX_LIST_CACHE[group] = out
    return out


def _index_list(group: str) -> list:
    group = (group or "broad").lower()
    if group == "broad":
        return INDEX_BROAD
    if group in ("industry", "concept"):
        return _ths_index_list(group)
    return []


def _index_name(kind: str, code: str) -> str:
    if kind == "broad":
        for it in INDEX_BROAD:
            if it["code"] == code:
                return it["name"]
        return code
    group = "industry" if kind == "ind" else "concept"
    for it in _ths_index_list(group):
        if it["code"] == code:
            return it["name"]
    return code


def _search_index(kw: str, limit: int = 30) -> list:
    kw = str(kw or "").strip()
    if not kw:
        return []
    q = kw.lower()

    def rank(items):
        alias_items = []
        target = INDEX_ALIASES.get(kw)
        if target:
            alias_items = [it for it in items if it["name"] == target]
        buckets = [[], [], [], []]
        for it in items:
            n = it["name"].lower()
            c = it["code"].lower()
            if n == q or c == q:
                buckets[0].append(it)
            elif n.startswith(q) or c.startswith(q):
                buckets[1].append(it)
            elif q in n or q in c:
                buckets[2].append(it)
            elif n in q:  # 输入更长，如「半导体设备」匹配「半导体」
                buckets[3].append(it)
        out, seen = [], set()
        for bucket in [alias_items] + buckets:
            for it in bucket:
                key = (it.get("kind"), it["code"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(it)
                if len(out) >= limit:
                    return out
        return out

    # 宽基指数很快，优先；命中就不去拉同花顺行业/概念列表
    broad = rank([dict(it, kind="broad", group="broad") for it in INDEX_BROAD])
    if broad:
        return broad
    return rank(_ths_index_list("industry") + _ths_index_list("concept"))


def _fnum(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x else 0.0  # NaN -> 0


def _bars_index(kind: str, code: str):
    """指数日K：broad 用新浪；ind/con 用同花顺行业/概念指数。"""
    key = (kind, code)
    if key in _IDX_KLINE_CACHE:
        return _IDX_KLINE_CACHE[key]
    bars = []
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        if kind == "broad":
            df = ak.stock_zh_index_daily(symbol=code)
            for _, r in df.iterrows():
                d = r["date"]
                ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                bars.append([ds, round(_fnum(r["open"]), 3), round(_fnum(r["high"]), 3),
                             round(_fnum(r["low"]), 3), round(_fnum(r["close"]), 3),
                             _fnum(r["volume"])])
        else:
            name = _index_name(kind, code)
            if name and name != code:
                fn = ak.stock_board_industry_index_ths if kind == "ind" else ak.stock_board_concept_index_ths
                df = fn(symbol=name, start_date="20200101", end_date=end)
                for _, r in df.iterrows():
                    d = r["日期"]
                    ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                    bars.append([ds, round(_fnum(r["开盘价"]), 3), round(_fnum(r["最高价"]), 3),
                                 round(_fnum(r["最低价"]), 3), round(_fnum(r["收盘价"]), 3),
                                 _fnum(r["成交量"])])
    except Exception:
        bars = []
    _IDX_KLINE_CACHE[key] = bars
    return bars


def _load_lines() -> dict:
    try:
        with open(LINES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("lines")
    return inner if isinstance(inner, dict) else data


def _save_key_lines(key: str, lines: list) -> None:
    with _LINES_LOCK:
        data = _load_lines()
        data[str(key)] = lines
        with open(LINES_FILE, "w", encoding="utf-8") as f:
            json.dump({"lines": data}, f, ensure_ascii=False, indent=2)


def _new_trade_id() -> str:
    return "T" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()


def _load_trades() -> list:
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return []
    recs = data.get("records")
    if not isinstance(recs, list):
        return []
    # 老记录没有 ID：用时间戳派生一个稳定的 ID
    for r in recs:
        if isinstance(r, dict) and not r.get("id"):
            try:
                r["id"] = "T" + str(int(r.get("ts") or 0))
            except Exception:
                r["id"] = "T0"
    return recs


def _save_trade_record(rec: dict):
    if not rec.get("id"):
        rec["id"] = _new_trade_id()
    with _LINES_LOCK:
        records = _load_trades()
        records.append(rec)
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump({"records": records}, f, ensure_ascii=False, indent=2)
    return len(records), rec["id"]


def _clean_line(ln: dict):
    t = ln.get("type")
    try:
        if t == "h":
            return {"type": "h", "y": round(float(ln.get("y") or 0), 4)}
        if t == "trend":
            return {"type": "trend", "x1": str(ln.get("x1"))[:32], "y1": round(float(ln.get("y1") or 0), 4),
                    "x2": str(ln.get("x2"))[:32], "y2": round(float(ln.get("y2") or 0), 4)}
        if t == "v":
            return {"type": "v", "x": str(ln.get("x"))[:32],
                    "y1": round(float(ln.get("y1") or 0), 4), "y2": round(float(ln.get("y2") or 0), 4)}
    except (TypeError, ValueError):
        return None
    return None


# ===================== 估值（PE/PB/PS 日频 + 历史分位） =====================
VAL_DIR = os.path.join(DATA_DIR, "valuation")
VAL_TTL = 6 * 3600          # 6 小时；日频数据，盘中不必重复拉
VAL_WINDOW = 1250           # 约 5 年交易日
VAL_MIN_N = 250             # 少于此样本量不算分位
VAL_COLS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,PE_TTM,PB_MRQ,PS_TTM"

_VAL_MEM: dict = {}
_VAL_LOCK = threading.Lock()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fetch_val_rows(code: str):
    """东财每日估值历史。返回 (名称, [[date, pe, pb, ps], ...] 升序)。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    base = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": VAL_COLS,
        "filter": f'(SECURITY_CODE="{code}")',
        "sortColumns": "TRADE_DATE",
        "sortTypes": -1,
        "source": "WEB",
        "client": "WEB",
        "pageSize": 1000,
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    rows, name, page, total = [], "", 1, None
    while True:
        r = requests.get(url, params=dict(base, pageNumber=page), headers=headers, timeout=20)
        r.raise_for_status()
        res = (r.json() or {}).get("result")
        if not res:
            break
        total = int(res.get("count") or 0) if total is None else total
        data = res.get("data") or []
        if not data:
            break
        for it in data:
            name = name or str(it.get("SECURITY_NAME_ABBR") or "")
            d = str(it.get("TRADE_DATE") or "")[:10]
            if d:
                rows.append([d, _num(it.get("PE_TTM")), _num(it.get("PB_MRQ")), _num(it.get("PS_TTM"))])
        if page >= int(res.get("pages") or 1) or len(rows) >= total:
            break
        page += 1
        time.sleep(0.2)
    rows.reverse()
    return name, rows


def _pct_series(vals, window: int = VAL_WINDOW, min_n: int = VAL_MIN_N):
    """滚动百分位：每个点只用它之前 window 个交易日（含当日），只用正值。"""
    out, win, q = [], [], deque()
    for v in vals:
        ok = v is not None and v > 0
        if ok:
            insort(win, v)
            q.append(v)
        else:
            q.append(None)
        if len(q) > window:
            old = q.popleft()
            if old is not None:
                k = bisect_left(win, old)
                if k < len(win) and win[k] == old:
                    win.pop(k)
        out.append(round(bisect_right(win, v) / len(win) * 100, 1)
                   if ok and len(win) >= min_n else None)
    return out


def _valuation_payload(code: str) -> dict:
    now = time.time()
    with _VAL_LOCK:
        mem = _VAL_MEM.get(code)
        if mem and now - mem["at"] < VAL_TTL:
            return mem["payload"]

    os.makedirs(VAL_DIR, exist_ok=True)
    path = os.path.join(VAL_DIR, f"{code}.json")
    payload = None
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < VAL_TTL:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
    except Exception:
        payload = None

    if payload is None:
        name, rows = _fetch_val_rows(code)
        if not rows:
            payload = {"code": code, "name": name, "rows": [],
                       "reason": "该标的无 PE/PB/PS（ETF/指数或数据缺失）"}
        else:
            pe_p = _pct_series([r[1] for r in rows])
            pb_p = _pct_series([r[2] for r in rows])
            ps_p = _pct_series([r[3] for r in rows])
            out = []
            for i, r in enumerate(rows):
                out.append([
                    r[0],
                    None if r[1] is None else round(r[1], 2),
                    None if r[2] is None else round(r[2], 2),
                    None if r[3] is None else round(r[3], 2),
                    pe_p[i], pb_p[i], ps_p[i],
                ])
            payload = {"code": code, "name": name, "rows": out,
                       "basis": VAL_WINDOW, "min_n": VAL_MIN_N,
                       "start": rows[0][0], "end": rows[-1][0],
                       "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass

    with _VAL_LOCK:
        _VAL_MEM[code] = {"at": now, "payload": payload}
    return payload


# ===================== 商品（布伦特/黄金/白银…） =====================
CMD_TTL = 6 * 3600
_CMD_MEM: dict = {}
_CMD_LOCK = threading.Lock()


def _cmd_list():
    return [{"code": c["code"], "name": c["name"], "market": "cmd", "unit": c["unit"]}
            for c in COMMODITIES]


def _cmd_name(code):
    c = _CMD_BY_CODE.get(str(code).strip().upper())
    return c["name"] if c else ""


def _search_cmd(kw):
    q = (kw or "").strip().lower()
    if not q:
        return _cmd_list()
    out = []
    for c in COMMODITIES:
        hay = " ".join([c["code"].lower(), c["name"].lower()] +
                       [a.lower() for a in c["alias"]])
        if q in hay:
            out.append({"code": c["code"], "name": c["name"],
                        "market": "cmd", "unit": c["unit"]})
    return out[:20]


def _fetch_cmd_bars(symbol: str):
    """新浪全球期货日线。返回 [[ISO时间, o, h, l, c, v], ...] 升序。"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           "var%20_=/GlobalFuturesService.getGlobalFuturesDailyKLine")
    r = requests.get(url, params={"symbol": symbol},
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://finance.sina.com.cn/"},
                     timeout=20)
    r.raise_for_status()
    text = r.text
    a, b = text.find("("), text.rfind(")")
    if a < 0 or b <= a:
        return []
    rows = json.loads(text[a + 1:b])
    out = []
    for it in rows:
        try:
            d = str(it.get("date") or "")[:10]
            o = float(it["open"])
            h = float(it["high"])
            l = float(it["low"])
            c = float(it["close"])
            if not d or c <= 0 or o <= 0:
                continue
            # 新浪当日部分数据偶尔 open 跑到 high 之外，做一次修正
            h = max(h, o, c)
            l = min(l, o, c)
            v = float(it.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        out.append([d + "T00:00:00", o, h, l, c, v])
    ded = {row[0]: row for row in out}
    return [ded[k] for k in sorted(ded)]


def _bars_cmd(code: str):
    code = str(code).strip().upper()
    now = time.time()
    with _CMD_LOCK:
        mem = _CMD_MEM.get(code)
        if mem and now - mem["at"] < CMD_TTL:
            return mem["bars"]

    os.makedirs(CMD_DIR, exist_ok=True)
    path = os.path.join(CMD_DIR, f"{code}.json")
    if os.path.exists(path) and now - os.path.getmtime(path) < CMD_TTL:
        try:
            with open(path, encoding="utf-8") as f:
                bars = json.load(f)
            with _CMD_LOCK:
                _CMD_MEM[code] = {"at": now, "bars": bars}
            return bars
        except Exception:
            pass

    try:
        bars = _fetch_cmd_bars(code)
    except Exception:
        bars = []
    if not bars:                      # 拉取失败：退回旧缓存
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bars, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass
    with _CMD_LOCK:
        _CMD_MEM[code] = {"at": now, "bars": bars}
    return bars


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send_html(200, HTML)
        if u.path == "/api/boards":
            boards = []
            try:
                boards = [{"code": b["code"], "name": b["name"]} for b in get_concept_boards()]
            except Exception:
                boards = []
            if not boards:
                boards = [{"code": b["code"], "name": b["name"]} for b in FALLBACK_BOARDS]
            return self._send(200, boards)
        if u.path == "/api/constituents":
            q = parse_qs(u.query)
            code = q.get("board", [""])[0]
            if code.startswith("fb_"):
                fb = next((b for b in FALLBACK_BOARDS if b["code"] == code), None)
                return self._send(200, [{"code": c, "name": n}
                                        for c, n in (fb["stocks"] if fb else [])])
            cons = []
            try:
                cons = [{"code": c["code"], "name": c["name"]} for c in get_board_constituents(code)]
            except Exception:
                cons = []
            if not cons:
                fb = next((b for b in FALLBACK_BOARDS if b["code"] == code), None)
                if fb:
                    cons = [{"code": c, "name": n} for c, n in fb["stocks"]]
            return self._send(200, cons)
        if u.path == "/api/kline":
            q = parse_qs(u.query)
            code = q.get("code", [""])[0].strip()
            if not code:
                return self._send(400, {"error": "no code"})
            market = (q.get("market", [""])[0] or "").lower()
            kind = (q.get("kind", [""])[0] or "").lower()
            try:
                if market == "cmd":
                    bars = _bars_cmd(code)
                    return self._send(200, {"code": code.upper(),
                                            "name": _cmd_name(code) or code,
                                            "bars": bars})
                if market == "idx" or kind in ("broad", "ind", "con"):
                    kind = kind or "broad"
                    if kind not in ("broad", "ind", "con"):
                        return self._send(400, {"error": "bad kind"})
                    bars = _bars_index(kind, code)
                    return self._send(200, {"code": code, "name": _index_name(kind, code), "bars": bars})
                if code.isdigit():
                    bars = _bars_cn(code)
                    return self._send(200, {"code": code, "name": _cn_name(code) or code, "bars": bars})
                bars = _bars_us(code)
                return self._send(200, {"code": code, "name": _us_name(code) or code, "bars": bars})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/index_list":
            q = parse_qs(u.query)
            group = (q.get("group", ["broad"])[0] or "broad").lower()
            try:
                return self._send(200, _index_list(group))
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/search_index":
            q = parse_qs(u.query)
            kw = (q.get("q", [""])[0] or "").strip()[:30]
            try:
                return self._send(200, _search_index(kw))
            except Exception:
                return self._send(200, [])
        if u.path == "/api/cmd_list":
            return self._send(200, _cmd_list())
        if u.path == "/api/search_cmd":
            q = parse_qs(u.query)
            kw = (q.get("q", [""])[0] or "").strip()[:30]
            try:
                return self._send(200, _search_cmd(kw))
            except Exception:
                return self._send(200, [])
        if u.path == "/api/us_list":
            return self._send(200, _us_list())
        if u.path == "/api/search_us":
            q = parse_qs(u.query)
            kw = (q.get("q", [""])[0] or "").strip()[:30]
            try:
                return self._send(200, _search_us(kw))
            except Exception:
                return self._send(200, [])
        if u.path == "/api/search":
            q = parse_qs(u.query)
            kw = (q.get("q", [""])[0] or "").strip()[:30]
            market = (q.get("market", ["cn"])[0] or "cn").lower()
            if market != "cn":
                return self._send(200, [])
            try:
                extra = _search_broad(kw)
                if extra:
                    return self._send(200, extra)
                return self._send(200, _search_cn(kw))
            except Exception:
                return self._send(200, [])
        if u.path == "/api/valuation":
            q = parse_qs(u.query)
            code = q.get("code", [""])[0].strip()
            if not code.isdigit():
                return self._send(200, {"code": code, "rows": [],
                                        "reason": "估值仅支持 A 股代码"})
            try:
                return self._send(200, _valuation_payload(code))
            except Exception as e:
                return self._send(200, {"code": code, "rows": [],
                                        "reason": "估值获取失败：" + str(e)[:80]})
        if u.path == "/api/lines":
            return self._send(200, {"lines": _load_lines()})
        if u.path == "/api/trades":
            records = _load_trades()
            records.sort(key=lambda r: r.get("ts", 0), reverse=True)
            return self._send(200, {"records": records[:200]})
        if u.path == "/api/trade":
            q = parse_qs(u.query)
            tid = (q.get("id", [""])[0] or "").strip()
            for rec in _load_trades():
                if str(rec.get("id") or "") == tid:
                    return self._send(200, {"ok": True, "record": rec})
            return self._send(404, {"ok": False, "error": "record not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/lines":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                key = str(d.get("key") or "").strip()
                lines = d.get("lines")
                if not key or not isinstance(lines, list):
                    return self._send(400, {"error": "key/lines invalid"})
                clean = []
                for ln in lines[:500]:
                    c = _clean_line(ln)
                    if c:
                        clean.append(c)
                _save_key_lines(key, clean)
                return self._send(200, {"ok": True, "n": len(clean)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/delete_trade":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                tid = str(d.get("id") or "").strip()
                ts = int(d.get("ts") or 0)
                if not tid and not ts:
                    return self._send(400, {"error": "id/ts required"})
                with _LINES_LOCK:
                    records = _load_trades()
                    kept = [r for r in records
                            if not ((tid and str(r.get("id") or "") == tid)
                                    or (ts and int(r.get("ts") or 0) == ts))]
                    if len(kept) == len(records):
                        return self._send(404, {"error": "记录不存在"})
                    with open(TRADES_FILE, "w", encoding="utf-8") as f:
                        json.dump({"records": kept}, f, ensure_ascii=False, indent=2)
                return self._send(200, {"ok": True, "n": len(kept)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/save_trades":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                code = str(d.get("code") or "").strip()
                if not code:
                    return self._send(400, {"error": "no code"})
                rec = {
                    "id": _new_trade_id(),
                    "ts": int(time.time()),
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "market": str(d.get("market") or ""),
                    "kind": str(d.get("kind") or ""),
                    "unit": str(d.get("unit") or "half"),
                    "code": code,
                    "name": str(d.get("name") or ""),
                    "start": str(d.get("start") or ""),
                    "startTime": str(d.get("startTime") or ""),
                    "end": str(d.get("end") or ""),
                    "endTime": str(d.get("endTime") or ""),
                    "trades": d.get("trades") if isinstance(d.get("trades"), list) else [],
                    "roundTrips": d.get("roundTrips") if isinstance(d.get("roundTrips"), list) else [],
                    "finalEquity": d.get("finalEquity"),
                    "finalCash": d.get("finalCash"),
                    "finalShares": d.get("finalShares"),
                    "finalReturn": d.get("finalReturn"),
                    "maxDD": d.get("maxDD"),
                }
                total, tid = _save_trade_record(rec)
                return self._send(200, {"ok": True, "n": total, "id": tid})
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass


def _preload_lists():
    """后台预热股票/ETF/指数列表，避免第一次搜索卡很久。"""
    for fn in (_cn_stock_universe, _cn_etf_list):
        try:
            fn()
        except Exception:
            pass
    for group in ("industry", "concept"):
        try:
            _ths_index_list(group)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="回测游戏")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=_preload_lists, daemon=True).start()
    print(f"回测游戏已启动: http://{args.host}:{args.port}   (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
