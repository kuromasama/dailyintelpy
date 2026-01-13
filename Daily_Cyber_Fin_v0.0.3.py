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
FINANCE_LOG_FILE = "finance_log.md"

# ================= 模型配置 =================
MODEL_NAME = 'models/gemini-3-flash-preview'

# ================= 核心工具 =================

def get_tw_time():
    """ 取得台灣時間 (UTC+8) """
    return datetime.utcnow() + timedelta(hours=8)

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
    data["last_updated"] = get_tw_time().strftime("%Y-%m-%d %H:%M:%S")
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_stock_code(name_or_code, alias_dict):
    name_upper = name_or_code.upper()
    return alias_dict.get(name_upper, name_upper)

def send_telegram(token, message):
    if not token: print(f"[模擬發送] {message[:50]}..."); return
    
    # 手機版 TG：清洗 Markdown
    clean_message = message.replace("**", "").replace("##", "").replace("###", "").replace("__", "").replace("`", "")
    clean_message = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", clean_message)

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
    buffer = []; processed = []; 
    now_utc = datetime.utcnow() # 用 UTC 做基準
    
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

                date_prefix = ""
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    # 解析原始時間 (這是 UTC)
                    pub_time_utc = datetime(*entry.published_parsed[:6])
                    
                    # 過濾舊新聞 (UTC 對 UTC 比較，準確)
                    if (now_utc - pub_time_utc).total_seconds() > hours_limit * 3600: continue
                    
                    # v0.0.3 修正：日期標籤使用「原始 UTC 時間」，不轉台灣時間，以符合原始網頁顯示
                    date_prefix = f"[{pub_time_utc.strftime('%m/%d')}] "
                else:
                    # 若無時間，用當下 UTC
                    date_prefix = f"[{now_utc.strftime('%m/%d')}] "
                
                processed.append(entry.title)
                buffer.append(f"{date_prefix}標題: {entry.title}\n連結: {entry.link}\n")
        except: continue
    return "\n".join(buffer) if buffer else ""

def save_log(filename, content):
    # 存檔時使用台灣時間戳記，方便你整理
    timestamp = get_tw_time().strftime("%Y-%m-%d %H:%M")
    try:
        with open(filename, "a", encoding="utf-8") as f:
            # 這裡不加分隔線，讓 AI 的 Markdown 輸出直接接續，格式更自由
            f.write(f"\n\n{content}\n")
        print(f"💾 已存檔至 {filename}")
    except: pass

def is_market_open():
    return get_tw_time().weekday() < 5

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

# ================= 執行模式 (豐富版 V0.0.3) =================

def run_security_mode(config):
    """ 資安 Bot """
    token = TG_BOT_TOKEN_SEC
    print(f"🛡️ [資安 Bot] 啟動... ({MODEL_NAME})")
    today_str = get_tw_time().strftime("%Y/%m/%d")
    
    # 回溯與抓取
    history = read_history_log(SECURITY_LOG_FILE)
    time_limit = 168 if len(history) < 100 else 24
    urls = config.get("rss_security", [])
    raw = get_rss_data(urls, limit=10, hours_limit=time_limit, history_content=history)
    
    if not raw:
        print("✅ 無新進重要新聞")
        return

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    # 1. TG 訊息生成 (保持純文字，無 Markdown)
    print(" ↳ 生成 TG 訊息...")
    tg_prompt = f"""
    你是資安情報官。請整理以下情報。
    【內容】{raw}
    【格式要求】
    1. 繁體中文。
    2. 新聞列表：`[MM/DD]` 標題 (Emoji) + 連結。
    3. CISSP 微課程：針對最重要的一則新聞，進行技術解讀 (案例/紅隊攻擊/藍隊防禦)。
    4. **嚴禁 Markdown 粗體** (Telegram 手機版閱讀用)。
    """
    tg_content = model.generate_content(tg_prompt).text
    send_telegram(token, tg_content)
    
    # 2. Markdown 日報存檔 (V0.0.3 重點：火力全開的詳細版)
    print(" ↳ 生成詳細 MD 日報...")
    file_prompt = f"""
    你是 CISSP 資深顧問。請撰寫一份**完整的資安日報 (Markdown 格式)**。
    
    【輸入資料】
    {raw}
    
    【文件要求 - 請務必詳細，勿精簡】
    標題：# 🛡️ 資安戰情日報 ({today_str})
    
    ## 1. 🌍 全球威脅快報 (News Briefing)
    - 請列出所有新聞，並附上 `[MM/DD]` 日期與連結。
    - 每則新聞請多寫一行「影響評估」。
    
    ## 2. 🎓 CISSP 深度戰術分析 (Deep Dive)
    請挑選今日最具指標性的攻擊事件，進行深度剖析：
    - **📚 案例背景**：發生了什麼？
    - **🧠 CISSP 知識領域**：對應哪一個 Domain？
    - **⚔️ 紅隊視角 (Red Team)**：駭客使用了什麼 CVE？Payload 原理？(請詳細說明技術細節)
    - **🛡️ 藍隊視角 (Blue Team)**：企業架構師該如何防禦？(WAF 規則、Patch 策略、縱深防禦)
    
    (請使用豐富的 Markdown 排版：Bold, Lists, Code blocks)
    """
    save_log(SECURITY_LOG_FILE, model.generate_content(file_prompt).text)

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
    today_str = get_tw_time().strftime("%Y/%m/%d")

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
        clean_tech = tech_str.replace("**", "") 
        status = "🟢" if market_open else "🔴"
        clean_summary = summary_line.replace("**", "")
        msg = f"📊 **持股戰情牆** ({status})\n\n{clean_summary}\n\n{clean_tech}\n\n📝 **系統**: {' '.join(logs) if logs else '無交易'}"
        send_telegram(token, msg)
    
    # 2. TG 簡報 (純文字)
    print(" ↳ 生成 TG 簡報...")
    strategy_prompt = f"""
    你是 CFO 與技術導師。
    【持股】{tech_str}
    【新聞】{raw_news}
    【任務】
    1. **使用繁體中文**。
    2. 對持股進行利多/利空分析。
    3. K 線教學：解釋型態 (黃金交叉, 背離, 支撐壓力)。
    4. 嚴禁 Markdown。
    """
    send_telegram(token, model.generate_content(strategy_prompt).text)
    
    # 3. MD 日報 (詳細版)
    if mode == "finance":
        print(" ↳ 生成詳細 MD 日報...")
        file_prompt = f"""
        請撰寫一份**完整的投資策略日報 (Markdown 格式)**。
        【日期】{today_str}
        【持股數據】
        {tech_str}
        【市場新聞】
        {raw_news}
        
        【要求】
        1. 使用豐富的 Markdown 排版 (標題、列表、粗體)。
        2. **持股分析**：請詳細列出每一檔股票的技術面狀態。
        3. **新聞解讀**：請引用新聞並分析對台股/美股的影響。
        4. **技術教學**：請詳細解說今日的 K 線型態教學。
        """
        save_log(FINANCE_LOG_FILE, model.generate_content(file_prompt).text)

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