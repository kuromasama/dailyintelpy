import feedparser
import google.generativeai as genai
import requests
import os
import argparse
import json
import re
import yfinance as yf
from datetime import datetime, timedelta

# ================= 環境變數 =================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
TG_BOT_TOKEN_SEC = os.getenv("TG_BOT_TOKEN_SEC") 
TG_BOT_TOKEN_FIN = os.getenv("TG_BOT_TOKEN_FIN")

PORTFOLIO_FILE = "portfolio.json"
SECURITY_LOG_FILE = "security_log.md"

# ================= 模型配置 (3-Flash) =================
MODEL_NAME = 'models/gemini-3-flash-preview'

# ================= 核心工具 =================

def load_portfolio():
    default_data = {"holdings": {}, "watchlist": {}, "config": {"aliases": {}, "rss_security": [], "rss_finance_tw": [], "rss_finance_us": []}}
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "config" not in data: data["config"] = default_data["config"]
                return data
        except: return default_data
    return default_data

def save_portfolio(data):
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_stock_code(name_or_code, alias_dict):
    name_upper = name_or_code.upper()
    return alias_dict.get(name_upper, name_upper)

def send_telegram(token, message):
    if not token: print(f"[模擬發送] {message[:50]}..."); return
    
    # 🧼 V13.1 新增：強制清洗 Markdown 符號，確保 TG 顯示乾淨
    clean_message = message.replace("**", "").replace("##", "").replace("__", "").replace("`", "")
    
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": TG_CHAT_ID, "text": clean_message, "disable_web_page_preview": False 
        })
    except Exception as e: print(f"TG 發送失敗: {e}")

def read_history_log(filename):
    if not os.path.exists(filename): return ""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    except: return ""

def get_rss_data(urls, limit=10, hours_limit=24, history_content=""):
    buffer = []; processed = []; now = datetime.now()
    if not urls: return "無訂閱來源"
    
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if len(processed) >= limit: break
                
                # 去重
                if entry.title in processed: continue
                if history_content and (entry.link in history_content or entry.title in history_content):
                    continue 

                # 時間過濾
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_time = datetime(*entry.published_parsed[:6])
                    if (now - pub_time).total_seconds() > hours_limit * 3600: continue
                
                processed.append(entry.title)
                buffer.append(f"標題: {entry.title}\n連結: {entry.link}\n")
        except: continue
    return "\n".join(buffer) if buffer else ""

def save_log(filename, content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(f"\n\n# 📅 {timestamp}\n{content}\n---\n")
        print(f"💾 已存檔至 {filename}")
    except: pass

def is_market_open():
    return datetime.now().weekday() < 5

def get_stock_technical(code):
    try:
        ticker = code
        if code.isdigit(): ticker = f"{code}.TW"
        elif code == "NQ=F": ticker = "NQ=F"
        
        stock = yf.Ticker(ticker)
        hist = stock.history(period="3mo")
        if len(hist) < 20: return None
        
        price = round(hist['Close'].iloc[-1], 2)
        pct = round(((price - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100, 2)
        ma5 = round(hist['Close'].rolling(5).mean().iloc[-1], 2)
        ma20 = round(hist['Close'].rolling(20).mean().iloc[-1], 2)
        
        trend = "盤整 ⚖️"
        if price > ma5 > ma20: trend = "強勢多頭 🔥"
        elif price > ma20: trend = "多頭格局 📈"
        elif price < ma5 < ma20: trend = "空頭修正 📉"
        
        return {"price": price, "pct": pct, "trend": trend, "ma20": ma20}
    except: return None

# ================= 指令處理 =================
def process_tg_commands(token):
    print("📥 讀取指令...")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url).json()
        if "result" not in response: return [], load_portfolio()
        pf_data = load_portfolio()
        holdings = pf_data.get("holdings", {})
        watchlist = pf_data.get("watchlist", {})
        config = pf_data.get("config", {})
        aliases = config.get("aliases", {})
        logs = []
        is_updated = False
        
        for item in response["result"]:
            if str(item["message"]["chat"]["id"]) != str(TG_CHAT_ID): continue
            msg_time = datetime.fromtimestamp(item["message"]["date"])
            if datetime.now() - msg_time > timedelta(hours=24): continue
            text = item["message"].get("text", "").strip()
            
            if text.startswith("{"):
                try:
                    new_data = json.loads(text)
                    if "holdings" in new_data: pf_data = new_data; is_updated = True; logs.append("✅ JSON 重置成功")
                except: pass
                continue

            match_trade = re.search(r"(買進|賣出|Buy|Sell)\s+(\S+)\s+(\d+)\s*(\d+(?:\.\d+)?)?", text, re.IGNORECASE)
            if match_trade:
                action, name, shares, price = match_trade.groups()
                code = get_stock_code(name, aliases)
                shares = int(shares); price = float(price) if price else 0
                if code not in holdings: holdings[code] = {"name": name, "shares": 0, "avg_cost": 0, "current_price": price}
                curr = holdings[code]
                if action in ["買進", "Buy"]:
                    total = (curr["shares"]*curr["avg_cost"])+(shares*price)
                    curr["shares"] += shares
                    curr["avg_cost"] = round(total/curr["shares"], 2) if curr["shares"]>0 else 0
                    logs.append(f"✅ 買入 {name} {shares}股")
                elif action in ["賣出", "Sell"]:
                    curr["shares"] = max(0, curr["shares"]-shares)
                    logs.append(f"✅ 賣出 {name} {shares}股")
                if price > 0: curr["current_price"] = price
                is_updated = True
                continue

            match_watch = re.search(r"(關注|移除|Watch|Remove)\s+(\S+)", text, re.IGNORECASE)
            if match_watch:
                action, name = match_watch.groups()
                code = get_stock_code(name, aliases)
                if action in ["關注", "Watch"]:
                    market = "US" if re.match(r"^[A-Z=]+$", code) else "TW"
                    watchlist[code] = {"name": name, "market": market}
                    logs.append(f"👁️ 關注 {name}")
                else:
                    if code in watchlist: del watchlist[code]; logs.append(f"🗑️ 移除 {name}")
                is_updated = True
                continue

        if is_updated:
            config["aliases"] = aliases; pf_data["holdings"] = holdings
            pf_data["watchlist"] = watchlist; pf_data["config"] = config
            save_portfolio(pf_data)
        return logs, pf_data
    except: return [], load_portfolio()

# ================= 執行模式 (V13.1 - 繁中 & 乾淨排版) =================

def run_security_mode(config):
    """ 資安 Bot """
    token = TG_BOT_TOKEN_SEC
    print(f"🛡️ [資安 Bot] 啟動... ({MODEL_NAME})")
    
    # 7天回溯
    history = read_history_log(SECURITY_LOG_FILE)
    time_limit = 168 if len(history) < 100 else 24
    
    urls = config.get("rss_security", [])
    raw = get_rss_data(urls, limit=10, hours_limit=time_limit, history_content=history)
    
    if not raw:
        print("✅ 無新進重要新聞")
        return

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    # 1. 新聞快報 (繁體中文 + 說明)
    print(" ↳ 發送新聞快報...")
    news_prompt = f"""
    你是資安情報官。請整理以下情報。
    【內容】{raw}
    【格式要求】
    1. **必須使用繁體中文 (Traditional Chinese)**。
    2. 每個新聞請列出：
       - 標題 (Emoji: 🚨, 🛡️)
       - 📅 **事件背景**：(一句話解釋發生什麼事)
       - 🔗 **原始連結**：(必須附上)
    3. 嚴禁使用 Markdown 符號 (如 ** 或 ##)，請用 Emoji 排版。
    """
    send_telegram(token, model.generate_content(news_prompt).text)
    
    # 2. CISSP 深度教學 (繁體中文 + 專有名詞)
    print(" ↳ 發送 CISSP 微課程...")
    class_prompt = f"""
    你是 CISSP 資深教練。請針對今日新聞寫一份「深度技術分析」。
    【新聞】{raw}
    【格式嚴格要求】
    1. **全篇必須使用繁體中文**。
    2. 結構如下 (請使用 Emoji 當標題，不要用 Markdown)：
    
    🎓 **CISSP 實戰教練**
    
    📚 **案例事件**：(詳細說明事件背景)
    
    🧠 **CISSP 考點 & 名詞解釋**：(解釋專有名詞，如 Zero Trust, CVE, XSS, Buffer Overflow)
    
    ⚔️ **攻擊技術解構 (Red Team)**：
    (深入技術細節：駭客利用了什麼底層機制？Payload 是什麼概念？)
    
    🛡️ **防禦架構設計 (Blue Team)**：
    (企業級防禦建議：WAF, IPS, 分段, 權限控管)
    
    (嚴禁 Markdown 粗體符號)
    """
    send_telegram(token, model.generate_content(class_prompt).text)
    
    # 3. 寫入 Log
    save_log(SECURITY_LOG_FILE, model.generate_content(f"CISSP 日報 (繁中)\n{raw}").text)

def run_morning_forecast(pf_data):
    token = TG_BOT_TOKEN_FIN
    print(f"📈 [早報 Bot] 啟動...")
    config = pf_data.get("config", {})
    holdings = pf_data.get("holdings", {})
    watchlist = pf_data.get("watchlist", {})
    
    targets = [f"{v['name']}" for c, v in holdings.items() if v['shares']>0] + [f"{v['name']}" for c, v in watchlist.items()]
    urls = config.get("rss_finance_us", [])
    raw_us = get_rss_data(urls, limit=5, hours_limit=24) 
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = f"華爾街操盤手簡報。新聞:{raw_us}\n關注:{', '.join(targets)}\n任務:美股收盤簡報+台股開盤預測。\n要求: 繁體中文, 純文字, Emoji 排版。"
    send_telegram(token, model.generate_content(prompt).text)

def run_finance_mode(pf_data, mode="finance"):
    token = TG_BOT_TOKEN_FIN
    print(f"💰 [財經 Bot] 啟動...")
    logs, pf_data = process_tg_commands(token)
    config = pf_data.get("config", {})
    holdings = pf_data.get("holdings", {})
    watchlist = pf_data.get("watchlist", {})
    
    market_open = is_market_open()
    tech_lines = []
    
    total_market_value = 0
    total_cost = 0
    
    if market_open:
        for code in {**holdings, **watchlist}:
            is_holding = holdings.get(code, {}).get('shares', 0) > 0
            is_watching = code in watchlist
            
            if is_holding or is_watching:
                t = get_stock_technical(code)
                if t:
                    name = holdings.get(code, {}).get('name') or watchlist.get(code, {}).get('name')
                    price = t['price']
                    if code in holdings: holdings[code]['current_price'] = price
                    
                    detail_str = ""
                    if is_holding:
                        shares = holdings[code]['shares']
                        avg_cost = holdings[code]['avg_cost']
                        market_val = int(shares * price)
                        cost_basis = int(shares * avg_cost)
                        unrealized_pl = market_val - cost_basis
                        roi = round((unrealized_pl / cost_basis * 100), 2) if cost_basis > 0 else 0
                        
                        total_market_value += market_val
                        total_cost += cost_basis
                        detail_str = f"\n   📦 **庫存**: {shares} | 均價: {avg_cost}\n   💰 **損益**: ${unrealized_pl:,} ({roi}%)"
                    
                    tech_lines.append(f"🔹 **{name} ({code})**\n   📈 現價: {price} ({t['pct']}%){detail_str}\n   📊 {t['trend']}")
        save_portfolio(pf_data)
    
    total_pl = total_market_value - total_cost
    total_roi = round((total_pl / total_cost * 100), 2) if total_cost > 0 else 0
    summary_line = f"🏆 **總資產**: ${total_market_value:,} | **總損益**: ${total_pl:,} ({total_roi}%)" if total_cost > 0 else ""
    
    tech_str = "\n".join(tech_lines) if tech_lines else "無報價數據"
    urls = config.get("rss_finance_tw", [])
    raw_news = get_rss_data(urls, limit=5, hours_limit=24)
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    # 1. 持股戰情牆 (TG)
    if tech_lines:
        clean_tech = tech_str.replace("**", "") # 雙重保險
        status = "🟢" if market_open else "🔴"
        clean_summary = summary_line.replace("**", "")
        msg = f"📊 **持股戰情牆** ({status})\n\n{clean_summary}\n\n{clean_tech}\n\n📝 **系統**: {' '.join(logs) if logs else '無交易'}"
        send_telegram(token, msg)
    
    # 2. 新聞分析與教學 (TG)
    print(" ↳ 發送新聞與教學...")
    strategy_prompt = f"""
    你是 CFO 與技術導師。
    【持股狀態】
    {tech_str}
    【新聞】
    {raw_news}
    【任務】
    1. **使用繁體中文**。
    2. 新聞分析：對持股是利多還是利空？
    3. K 線教學：挑一支股票，解釋型態 (黃金交叉, 背離, 支撐壓力)。
    4. 嚴禁 Markdown。
    """
    send_telegram(token, model.generate_content(strategy_prompt).text)
    
    if mode == "finance":
        save_log("finance_log.md", model.generate_content(f"完整投資日報\n{tech_str}\n{raw_news}").text)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, required=True, choices=["security", "finance", "morning", "manual"])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    pf_data = load_portfolio()
    if not GEMINI_API_KEY: exit(1)

    if args.mode == "security":
        run_security_mode(pf_data.get("config", {}))
    elif args.mode == "finance":
        run_finance_mode(pf_data, "finance")
    elif args.mode == "morning":
        run_morning_forecast(pf_data)
    elif args.mode == "manual":
        send_telegram(TG_BOT_TOKEN_FIN, "📜 指令表: 買進/賣出/關注/設定別名")