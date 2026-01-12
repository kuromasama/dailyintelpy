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

# ================= 模型配置 (V12.0 穩定版) =================
# 3-Flash 只有 20次/天，這裡改回 2.0-Flash (1500次/天) 確保穩定不當機
MODEL_NAME = 'models/gemini-2.0-flash'

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
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": TG_CHAT_ID, "text": message, "disable_web_page_preview": False 
        })
    except Exception as e: print(f"TG 發送失敗: {e}")

def read_history_log(filename):
    """ 讀取歷史 Log 檔的內容，用於去重 """
    if not os.path.exists(filename): return ""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    except: return ""

def get_rss_data(urls, limit=10, hours_limit=168, history_content=""):
    """
    抓取 RSS，並根據歷史紀錄去重
    hours_limit 預設改為 168 (7天)，確保空白時能抓一週資料
    """
    buffer = []; processed = []; now = datetime.now()
    if not urls: return "無訂閱來源"
    
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if len(processed) >= limit: break
                
                # 1. 本次執行去重
                if entry.title in processed: continue
                
                # 2. 歷史檔案去重 (檢查 Link 或 Title 是否已存在於 log 檔中)
                # 簡單字串比對：如果連結已在 markdown 裡，就跳過
                if entry.link in history_content or entry.title in history_content:
                    continue 

                # 3. 時間過濾
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_time = datetime(*entry.published_parsed[:6])
                    if (now - pub_time).total_seconds() > hours_limit * 3600: continue
                
                processed.append(entry.title)
                buffer.append(f"標題: {entry.title}\n連結: {entry.link}\n")
        except: continue
    
    return "\n".join(buffer) if buffer else "" # 若都重複或無新聞，回傳空字串

def save_log(filename, content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        # 使用 'a' (append) 模式附加到檔案最後
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
        ma60 = round(hist['Close'].rolling(60).mean().iloc[-1], 2) if len(hist) >= 60 else 0
        
        trend = "盤整 ⚖️"
        if price > ma5 > ma20: trend = "強勢多頭 🔥"
        elif price > ma20: trend = "多頭格局 📈"
        elif price < ma5 < ma20: trend = "空頭修正 📉"
        
        return {"price": price, "pct": pct, "trend": trend, "ma5": ma5, "ma20": ma20, "ma60": ma60}
    except: return None

# ================= 指令處理 (維持不變) =================
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

# ================= 執行模式 (歷史比對版) =================

def run_security_mode(config):
    """ 資安 Bot """
    token = TG_BOT_TOKEN_SEC
    print(f"🛡️ [資安 Bot] 啟動... ")
    
    # 1. 讀取歷史 Log
    history = read_history_log("security_log.md")
    
    # 2. 抓取新聞 (傳入 history 進行比對去重，時間範圍 7 天)
    urls = config.get("rss_security", [])
    raw = get_rss_data(urls, limit=8, hours_limit=168, history_content=history)
    
    if not raw:
        print("無新進新聞 (全部已在 Log 中或無資料)")
        return # 沒新東西就不發 TG，也不浪費 AI 額度

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    # 3. 發送新聞快報 (TG)
    news_prompt = f"""
    你是 Aaron 的資安戰略官。請整理以下【新進】資安情報。
    【內容】{raw}
    【TG 格式要求】
    1. 標題 Emoji (🚨, 🛡️, 🐛)。
    2. **每則新聞附上原始連結 (Link)**。
    3. 針對 Fortinet, Windows, VPN 關鍵字加強標註。
    (純文字，無 Markdown)
    """
    send_telegram(token, model.generate_content(news_prompt).text)
    
    # 4. CISSP 教學 (TG)
    class_prompt = f"""
    你是 CISSP 講師。請針對今日新聞寫微課程。
    【內容】{raw}
    【格式】
    🎓 **CISSP 實戰教練**
    📚 **案例**：(事件)
    🧠 **知識點**：(Domain X)
    ⚔️ **攻擊解構**：(技術原理)
    🛡️ **防禦架構**：(縱深防禦策略)
    (純文字 + Emoji)
    """
    send_telegram(token, model.generate_content(class_prompt).text)
    
    # 5. 寫入 Log (這裡寫入後，下次執行就會被當作歷史紀錄)
    save_log("security_log.md", model.generate_content(f"CISSP 完整日報\n{raw}").text)

def run_morning_forecast(pf_data):
    token = TG_BOT_TOKEN_FIN
    print(f"📈 [早報 Bot] 啟動...")
    config = pf_data.get("config", {})
    holdings = pf_data.get("holdings", {})
    watchlist = pf_data.get("watchlist", {})
    
    targets = [f"{v['name']}" for c, v in holdings.items() if v['shares']>0] + [f"{v['name']}" for c, v in watchlist.items()]
    urls = config.get("rss_finance_us", [])
    raw_us = get_rss_data(urls, limit=5, hours_limit=24) # 美股保持 24h
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = f"你是華爾街操盤手。美股新聞:{raw_us}\n關注:{', '.join(targets)}\n任務:美股收盤簡報+台股開盤預測(Emoji,手機版)"
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
    if market_open:
        for code in {**holdings, **watchlist}:
            if code in watchlist or holdings.get(code, {}).get('shares', 0) > 0:
                t = get_stock_technical(code)
                if t:
                    name = holdings.get(code, {}).get('name') or watchlist.get(code, {}).get('name')
                    if code in holdings: holdings[code]['current_price'] = t['price']
                    tech_lines.append(f"🔹 **{name} ({code})**\n   💰 {t['price']} ({t['pct']}%)\n   📊 {t['trend']} | MA20:{t['ma20']}")
        save_portfolio(pf_data)
    
    tech_str = "\n".join(tech_lines) if tech_lines else "無報價"
    urls = config.get("rss_finance_tw", [])
    raw_news = get_rss_data(urls, limit=5, hours_limit=24)
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    status_emoji = "🟢" if market_open else "🔴"
    if tech_lines:
        send_telegram(token, f"📊 **持股戰情牆** ({status_emoji})\n\n{tech_str}\n\n📝 **系統**:\n" + ("\n".join(logs) if logs else "無新交易"))
    
    strategy_prompt = f"""
    你是 CFO。
    【持股】{tech_str}
    【新聞】{raw_news}
    【任務】
    1. 新聞分析與財報亮點。
    2. K 線實戰教學 (挑一支講)。
    (純文字+Emoji)
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