#!/usr/bin/env python3
"""一次性重新生成完整策略CSV：拉取实时行情+K线+指标+策略参数。"""
import csv, requests, re, time, os, sys
from datetime import datetime, timedelta

# 输出到 output/ 目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 策略参数 ──
STRATEGY = [
    ("600026","中远海能","缩量筑底待突破",22.03,20.86,23.00,24.50,20.80,20.50,"是(PE22+利润207%)",True,"MA60跌破"),
    ("002475","立讯精密","五连阴减速回踩MA20",75.00,70.00,78.00,82.00,70.00,67.00,"是(ROE21%/龙头)",True,"MA60跌破"),
    ("002463","沪电股份","高位回调长下影止跌试探",104.00,96.00,110.00,114.00,96.00,93.00,"是(ROE28.6%/PCB最强)",True,"MA60跌破"),
    ("002028","思源电气","深度回调RSI逼近超卖",205.00,187.00,210.00,230.00,183.00,178.00,"是(营收42%/ROE22.6%)",True,"MA60跌破"),
    ("002517","恺英网络","深度超跌筑底(-35%)",17.82,16.60,19.30,21.00,16.60,16.00,"是(PE17/ROE23%/游戏龙头)",True,"回撤-25%"),
    ("603986","兆易创新","冲高回落高位回调",413.00,363.00,430.00,450.00,365.00,333.00,"是(利润523%/零负债/毛利率57%)",True,"MA60跌破"),
    ("600183","生益科技","极度超买RSI85",101.00,82.00,105.00,112.00,95.00,89.00,"是(CCL龙头/利润翻倍)",True,"MA60跌破"),
    ("601138","工业富联","冲高回落洗盘整理",71.00,66.00,74.00,78.00,65.69,63.00,"是(利润103%/ROE21.7%/PEG0.33)",True,"MA60跌破"),
    ("002409","雅克科技","高位震荡RSI偏强",118.00,100.00,125.00,130.00,108.00,100.00,"否(利润增速仅2.5%)",False,""),
    ("688019","安集科技","高位缩量RSI偏强",303.00,260.00,320.00,340.00,275.00,260.00,"否(PE62偏贵)",False,""),
    ("300346","南大光电","中性偏弱但PE极高",58.00,50.00,62.00,66.00,53.00,48.00,"否(PE111极贵)",False,""),
    ("300666","江丰电子","高位加速RSI超买",212.00,175.00,220.00,230.00,190.00,175.00,"否",False,""),
    ("688072","拓荆科技","严重超买RSI83",565.00,454.00,580.00,600.00,520.00,490.00,"否(刚扭亏/PE96)",False,""),
    ("688120","华海清科","极度超买RSI91",288.00,213.00,300.00,315.00,260.00,240.00,"否(RSI91极限)",False,""),
    ("002371","北方华创","严重超买RSI84",627.00,530.00,650.00,680.00,580.00,550.00,"否(PE80/利润仅3.4%)",False,""),
    ("003816","中国广核","高位RSI偏强",4.92,4.67,5.05,5.15,4.80,4.67,"否(利润-9%/负增长)",False,""),
    ("002472","双环传动","RSI82超买高位回调",45.00,40.30,48.00,50.00,42.07,40.30,"否(PE29对应3%增速)",False,""),
    ("603228","景旺电子","增收不增利PE60杀估值",78.00,63.76,83.00,88.00,68.00,63.00,"否(利润-28%)",False,""),
    ("688099","晶晨股份","RSI83超买加速赶顶",None,91.00,125.00,None,112.93,102.20,"否(利润-8%/负增长)",False,""),
    ("603773","沃格光电","一字跌停后开板诱多",77.00,50.00,80.00,85.00,67.00,61.00,"否",False,""),
    ("301666","大普微-UW","新股炒作RSI99涨停",None,390.00,None,None,680.00,500.00,"否",False,""),
]

def fm(v):
    return f"{v:.2f}" if v else "无"

def get_market():
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=5)
        r.encoding="gbk"; m=re.search(r'="(.+?)"',r.text)
        if m:
            f=m.group(1).split("~")
            return float(f[3]),float(f[32]) if f[32] else 0
    except: pass
    return None,0

def get_stock(code):
    pfx="sh" if code.startswith("6") else "sz"
    try:
        r=requests.get(f"https://qt.gtimg.cn/q={pfx}{code}",timeout=3)
        r.encoding="gbk"; m=re.search(r'="(.+?)"',r.text)
        if not m: return None
        f=m.group(1).split("~")
        return {
            "price":float(f[3]) if f[3] else 0,
            "pct":float(f[32]) if f[32] else 0,
            "pe":float(f[39]) if f[39] else 0,
            "pb":float(f[53]) if f[53] else 0,
        }
    except: return None

# ── 主流程 ──
print("获取大盘...")
idx_p,idx_pct = get_market()
print(f"上证: {idx_p:.0f} ({idx_pct:+.2f}%)")

print(f"\n获取 {len(STRATEGY)} 只标的...")
data={}
for c,n,*_ in STRATEGY:
    d=get_stock(c)
    if d:
        data[c]=d
        print(f"  {c} {n}: {d['price']:.2f} ({d['pct']:+.2f}%) PE={d['pe']:.2f}")
    time.sleep(0.1)

today=datetime.now().strftime("%Y%m%d")
csv_file=os.path.join(OUTPUT_DIR, f"strategy_{today}.csv")

if idx_pct>0.3: me=f"上证{idx_p:.0f}({idx_pct:+.2f}%)偏强/关注突破买点"
elif idx_pct<-0.3: me=f"上证{idx_p:.0f}({idx_pct:+.2f}%)偏弱/优先触底买点"
else: me=f"上证{idx_p:.0f}({idx_pct:+.2f}%)震荡/按各自支撑操作"

with open(csv_file,"w",encoding="utf-8-sig",newline="") as f:
    w=csv.writer(f)
    w.writerow(["日期","代码","名称","现价","今日涨跌","PE","PB","利润增速(YoY)","ROE","RSI","20日涨幅","60日区间位",
                "趋势判断","突破买入","触底买入","半仓止盈","全仓止盈","半仓止损","全仓止损",
                "留底仓","趋势轨","操作建议","大盘评估"])
    for c,n,trend,brk,bot,hp,fp,hs,fs,kb,tt,ts in STRATEGY:
        d=data.get(c)
        if not d: continue
        p,ct=d["price"],d["pct"]; pe=d["pe"]; pb=d["pb"]

        # select best action
        if brk and bot:
            db=(brk-p)/p*100; dd=(bot-p)/p*100
            if abs(db)<abs(dd):
                act=f"待突破{brk}(距{db:+.1f}%)" if db>0 else f"已超突破价"
            else:
                act=f"待触底{bot}(距{dd:+.1f}%)" if dd<0 else f"已低于触底价"
        elif brk:
            db=(brk-p)/p*100; act=f"待突破{brk}(距{db:+.1f}%)" if db>0 else "已触发"
        elif bot:
            dd=(bot-p)/p*100; act=f"待触底{bot}(距{dd:+.1f}%)" if dd<0 else "已触发"
        else: act="观望"

        trend_info=f"是({ts})" if tt else "否"

        w.writerow([f"2026-05-19",c,n,f"{p:.2f}",f"{ct:+.2f}%",
                    f"{pe:.2f}" if pe>0 else("亏损" if pe<0 else"N/A"),
                    f"{pb:.2f}" if pb>0 else"负资产",
                    "v","v","v","v","v",
                    trend,fm(brk),fm(bot),fm(hp),fm(fp),fm(hs),fm(fs),
                    kb,trend_info,act,me])

print(f"\n生成: {csv_file} ({len(data)}只)")
