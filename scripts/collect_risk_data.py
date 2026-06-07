"""
AI Chain Risk Dashboard - Data Collection Script
Pulls public data for 68 indicators from free APIs.
Run: python scripts/collect_risk_data.py
Output: output/risk_data_YYYYMMDD.json
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta

# ============================================================
# CONFIG
# ============================================================
FRED_API_KEY = ""  # Optional: https://fred.stlouisfed.org/docs/api/api_key.html
OUTPUT_DIR = "output"
TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_YYMMDD = datetime.now().strftime("%Y%m%d")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# UTILS
# ============================================================
def fetch_url(url, headers=None, retries=2):
    """Fetch URL with retry."""
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if i == retries:
                return None
            time.sleep(1)

def fetch_json(url, headers=None):
    """Fetch JSON endpoint."""
    text = fetch_url(url, headers)
    if text:
        try:
            return json.loads(text)
        except:
            return None
    return None

def safe_float(v, default=None):
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("$", "").replace("T", "e12").replace("B", "e9"))
    except:
        return default

# ============================================================
# 1. FRED DATA (free, no API key required for basic access)
# ============================================================
class FREDCollector:
    """Collect data from FRED (Federal Reserve Economic Data)."""
    
    FRED_SERIES = {
        "BAMLH0A0HYM2": "HY OAS",          # High Yield Option-Adjusted Spread
        "DGS10": "10Y Treasury",            # 10-Year Treasury
        "T5YIE": "5Y Breakeven Inflation",  # 5-Year inflation expectations
    }
    
    def collect(self):
        results = {}
        for series_id, name in self.FRED_SERIES.items():
            # FRED allows direct JSON access without API key for latest observation
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=5"
            data = fetch_json(url)
            
            if data and "observations" in data:
                obs = data["observations"]
                for o in obs:
                    val = o.get("value")
                    if val and val != ".":
                        results[series_id] = {
                            "value": float(val),
                            "date": o.get("date"),
                            "name": name
                        }
                        break
            time.sleep(0.3)  # Rate limit
        return results

# ============================================================
# 2. YFINANCE - Stock Prices & Options
# ============================================================
def try_import_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        print("  [WARN] yfinance not installed. Run: pip install yfinance")
        return None

class StockCollector:
    """Collect stock prices, options, and fundamentals via yfinance."""
    
    TICKERS = {
        "NVDA": "NVIDIA",
        "SMCI": "Super Micro",
        "COHR": "Coherent",
        "LITE": "Lumentum",
        "AVGO": "Broadcom",
        "AMD": "AMD",
        "ANET": "Arista",
        "VRT": "Vertiv",
        "ETN": "Eaton",
        "DELL": "Dell",
        "CRM": "Salesforce",
        "NOW": "ServiceNow",
        "MSFT": "Microsoft",
        "9984.T": "SoftBank",
        "MU": "Micron",
        "EQIX": "Equinix",
    }
    
    # ETFs
    ETFS = {
        "QQQ": "Nasdaq 100 ETF",
        "QQEW": "Nasdaq 100 Equal Weight",
        "BOTZ": "AI & Robotics ETF",
        "AIQ": "Global X AI ETF",
    }
    
    def __init__(self):
        self.yf = try_import_yfinance()
    
    def collect_all(self):
        if not self.yf:
            return {"error": "yfinance not installed"}
        
        results = {"stocks": {}, "etfs": {}, "indices": {}}
        
        # Stock prices
        all_tickers = list(self.TICKERS.keys()) + list(self.ETFS.keys()) + ["^IXIC", "^GSPC"]
        try:
            data = self.yf.download(all_tickers, period="5d", progress=False)
            for ticker in all_tickers:
                if ticker in data.get("Close", {}):
                    closes = data["Close"][ticker].dropna()
                    if len(closes) >= 2:
                        current = float(closes.iloc[-1])
                        prev = float(closes.iloc[-2])
                        results["stocks"][ticker] = {
                            "price": round(current, 2),
                            "change_pct": round((current - prev) / prev * 100, 2),
                            "date": str(closes.index[-1].date())
                        }
        except Exception as e:
            results["error"] = str(e)
        
        # Individual ticker info for fundamentals
        for ticker in ["NVDA", "SMCI", "COHR", "AVGO", "AMD", "ANET", "VRT", "ETN", "DELL", "MU", "CRM", "NOW", "MSFT"]:
            try:
                t = self.yf.Ticker(ticker)
                info = t.info
                pe = info.get("trailingPE", None)
                results["stocks"][ticker] = results["stocks"].get(ticker, {})
                results["stocks"][ticker].update({
                    "pe": round(pe, 2) if pe else None,
                    "market_cap": info.get("marketCap", None),
                    "sector": info.get("sector", ""),
                })
            except:
                pass
        
        return results

# ============================================================
# 3. SLICKCHARTS - SP500 Weights
# ============================================================
class SP500WeightsCollector:
    """Scrape Slickcharts for Mag7 and Semiconductor weights."""
    
    MAG7_TICKERS = {"NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "TSLA"}
    SEMI_TICKERS = {"NVDA", "AVGO", "AMD", "MU", "INTC", "LRCX", "AMAT", "KLAC", 
                    "TXN", "ADI", "QCOM", "MPWR", "NXPI", "TER", "KEYS", "ON", "MCHP", "SWKS"}
    
    def collect(self):
        """Parse Slickcharts SP500 component weights."""
        html = fetch_url("https://www.slickcharts.com/sp500")
        if not html:
            return None
        
        results = {"mag7_weight": 0, "semi_weight": 0, "components": {}}
        
        # Parse table rows: pattern is symbol, weight%, price, chg%, %chg
        pattern = re.compile(
            r'<td[^>]*>\s*<a[^>]*>([A-Z]+(?:\.[A-Z]+)?)</a>\s*</td>\s*'
            r'<td[^>]*>([\d.]+)%</td>'
        )
        
        mag7_total = 0
        semi_total = 0
        
        for match in pattern.findall(html):
            ticker, weight = match
            w = float(weight)
            results["components"][ticker] = w
            
            if ticker in self.MAG7_TICKERS:
                mag7_total += w
            if ticker in self.SEMI_TICKERS:
                semi_total += w
        
        results["mag7_weight"] = round(mag7_total, 2)
        results["semi_weight"] = round(semi_total, 2)
        return results

# ============================================================
# 4. FINRA MARGIN DATA
# ============================================================
class FINRACollector:
    """Parse FINRA margin statistics."""
    
    def collect(self):
        """Get latest FINRA margin debt from YCharts or FINRA page."""
        # Use YCharts free page
        html = fetch_url("https://ycharts.com/indicators/finra_margin_debt")
        if not html:
            return None
        
        # Look for the latest value pattern
        # "Last Value" followed by number with T/B/M suffix
        m = re.search(r'Last Value\s*</[^>]*>\s*<[^>]*>\s*([\d.,]+)([TBM])', html)
        if not m:
            m = re.search(r'current level of ([\d.,]+)([TBM])', html)
        
        if m:
            value = float(m.group(1).replace(",", ""))
            unit = m.group(2)
            multiplier = {"T": 1e12, "B": 1e9, "M": 1e6}.get(unit, 1)
            margin_debt = value * multiplier
            
            # Find date
            date_m = re.search(r'(?:Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}', html)
            date = date_m.group(0) if date_m else None
            
            return {
                "margin_debt": margin_debt,
                "margin_debt_str": f"${value:.2f}{unit}",
                "date": date
            }
        return None


# ============================================================
# 5. FOREX - USD/CNY via free API
# ============================================================
class ForexCollector:
    """Get USD/CNY exchange rate."""
    
    def collect(self):
        """Use exchangerate-api or Yahoo."""
        # Try free API
        data = fetch_json("https://open.er-api.com/v6/latest/USD")
        if data and "rates" in data:
            cny = data["rates"].get("CNY")
            if cny:
                return {"usdcny": round(cny, 4), "date": data.get("time_last_update_utc", "")[:10]}
        return None


# ============================================================
# 6. OPTIONS P/C RATIO - NVDA
# ============================================================
class OptionsCollector:
    """Get NVDA put/call ratio from Yahoo Finance."""
    
    def collect(self):
        yf = try_import_yfinance()
        if not yf:
            return None
        try:
            nvda = yf.Ticker("NVDA")
            # Get options expiration dates
            options = nvda.options
            if not options:
                return None
            
            # Get nearest expiration
            exp = options[0]
            calls = nvda.option_chain(exp).calls
            puts = nvda.option_chain(exp).puts
            
            call_vol = calls["volume"].sum()
            put_vol = puts["volume"].sum()
            
            if call_vol > 0:
                return {
                    "put_call_ratio": round(put_vol / call_vol, 3),
                    "call_volume": int(call_vol),
                    "put_volume": int(put_vol),
                    "expiration": exp
                }
        except:
            pass
        return None


# ============================================================
# 7. VIX INDEX
# ============================================================
class VIXCollector:
    def collect(self):
        yf = try_import_yfinance()
        if not yf:
            return None
        try:
            data = yf.download("^VIX", period="5d", progress=False)
            if len(data) >= 2:
                return {
                    "vix": round(float(data["Close"].iloc[-1]), 2),
                    "change": round(float(data["Close"].iloc[-1]) - float(data["Close"].iloc[-2]), 2)
                }
        except:
            pass
        return None


# ============================================================
# MAIN COLLECTOR
# ============================================================
def collect_all():
    print(f"=== AI Chain Risk Data Collection ===")
    print(f"Date: {TODAY}")
    print()
    
    all_data = {
        "meta": {
            "collected_at": datetime.now().isoformat(),
            "data_date": TODAY,
            "version": "2.0-auto"
        },
        "indicators": {}
    }
    
    # 1. FRED
    print("[1/7] FRED economic data...")
    fred = FREDCollector().collect()
    if fred:
        for series_id, info in fred.items():
            print(f"  {info['name']}: {info['value']} ({info['date']})")
            n_map = {"BAMLH0A0HYM2": 4, "DGS10": 5, "T5YIE": 69}
            all_data["indicators"][f"fred_{series_id}"] = info
    else:
        print("  [WARN] FRED collection failed")
    
    # 2. Stock prices
    print("[2/7] Stock prices (yfinance)...")
    stocks = StockCollector().collect_all()
    if "error" not in stocks:
        all_data["stocks"] = stocks.get("stocks", {})
        print(f"  Collected {len(all_data['stocks'])} tickers")
    else:
        print(f"  [WARN] yfinance error: {stocks['error']}")
    
    # 3. SP500 weights
    print("[3/7] SP500 component weights (Slickcharts)...")
    sp500 = SP500WeightsCollector().collect()
    if sp500:
        print(f"  Mag7 weight: {sp500['mag7_weight']}%")
        print(f"  Semi weight: {sp500['semi_weight']}%")
        all_data["sp500"] = sp500
    else:
        print("  [WARN] Slickcharts scrape failed")
    
    # 4. FINRA margin
    print("[4/7] FINRA margin debt...")
    margin = FINRACollector().collect()
    if margin:
        print(f"  Margin debt: {margin['margin_debt_str']} ({margin['date']})")
        all_data["finra"] = margin
    else:
        print("  [WARN] FINRA data not found")
    
    # 5. Forex
    print("[5/7] USD/CNY exchange rate...")
    forex = ForexCollector().collect()
    if forex:
        print(f"  USD/CNY: {forex['usdcny']}")
        all_data["forex"] = forex
    else:
        print("  [WARN] Forex API failed")
    
    # 6. Options
    print("[6/7] NVDA options P/C ratio...")
    options = OptionsCollector().collect()
    if options:
        print(f"  NVDA P/C: {options['put_call_ratio']} (exp: {options['expiration']})")
        all_data["nvda_options"] = options
    else:
        print("  [WARN] Options data unavailable")
    
    # 7. VIX
    print("[7/7] VIX index...")
    vix = VIXCollector().collect()
    if vix:
        print(f"  VIX: {vix['vix']} ({vix['change']:+.2f})")
        all_data["vix"] = vix
    else:
        print("  [WARN] VIX data unavailable")
    
    # Save
    output_path = os.path.join(OUTPUT_DIR, f"risk_data_{TODAY_YYMMDD}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    
    print(f"\nSaved to {output_path}")
    
    # Summary
    print(f"\n=== Summary: What this script CAN collect ===")
    print(f"  HY OAS (#4), 10Y (#5), 5Y Breakeven (#69), VIX")
    print(f"  SP500 Mag7 weight (#62), Semi sector weight (#68)")
    print(f"  FINRA margin debt (#66 baseline)")
    print(f"  Stock prices for 16 AI-chain tickers")
    print(f"  USD/CNY (#57), NVDA P/C ratio (#23)")
    print(f"  SoftBank 9984.T (#56)")
    print(f"\n=== What this script CANNOT collect ===")
    print(f"  CDS prices (#1, #1b) - need Bloomberg/Markit")
    print(f"  Private valuations (#6, #7) - no public API")
    print(f"  Company financials (#11-35) - need SEC EDGAR XBRL parser")
    print(f"  AI startup metrics (#41-47) - no public data")
    print(f"  SemiAnalysis/TrendForce reports - paywalled")
    return all_data

if __name__ == "__main__":
    collect_all()
