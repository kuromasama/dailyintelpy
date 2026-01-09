import feedparser
import google.generativeai as genai
import requests
import os
import argparse
import json
import re
from datetime import datetime, timedelta

# ================= 環境變數 =================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

PORTFOLIO_FILE = "portfolio.json"

# ================= 核心工具 =================

def load_portfolio():
    default_data = {
        "holdings": {}, "watchlist": {}, 
        "config": {"aliases": {}, "rss_security": [], "rss_finance_tw": [], "rss_finance_us": []}
    }
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

def send_telegram(message):
    if not TG_BOT_TOKEN: 
        print(f"模擬發送 TG:\n{message}")
        return
    try:
        # 這裡依然不開 Markdown，靠 Prompt 控制排版
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json={
            "chat_id": TG_CHAT_ID, "text": message, "disable_web_page_preview": False # 開啟連結預覽
        })
        print("✅ TG 通知已發送")
    except Exception as e: print(f"TG 發送失敗: {e}")

def get_rss_data(urls, limit=3):
    buffer = []
    processed = []
    if not urls: return "無訂閱來源"
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit]:
                if entry.title in processed: continue
                processed.append(entry.title)
                link = entry.link
                buffer.append(f"- {entry.title} (Link: {link})")
        except: continue
    return "\n".join(buffer)

def save_log(filename, content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(f"\n\n# 📅 {timestamp}\n{content}\n---\n")
        print(f"💾 已存檔至 {filename}")
    except: pass

# ================= 邏輯處理 (含 TG 指令) =================

def process_tg_commands():
    print("📥 讀取 Telegram 指令...")
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
    
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
            
            if "指南" in text or "help" in text.lower():
                run_manual_guide()
                continue

            # 1. JSON 更新
            if text.startswith("{"):
                try:
                    new_data = json.loads(text)
                    if "holdings" in new_data:
                        pf_data = new_data
                        is_updated = True
                        logs.append("✅ 帳本已重置 (JSON)")
                except: pass
                continue

            # 2. 交易
            match_trade = re.search(r"(買進|賣出|Buy|Sell)\s+(\S+)\s+(\d+)\s*(\d+(?:\.\d+)?)?", text, re.IGNORECASE)
            if match_trade:
                action, name, shares, price = match_trade.groups()
                code = get_stock_code(name, aliases)
                shares = int(shares)
                price = float(price) if price else 0
                
                if code not in holdings:
                    holdings[code] = {"name": name, "shares": 0, "avg_cost": 0, "current_price": price}
                
                curr = holdings[code]
                if action in ["買進", "Buy"]:
                    total_cost = (curr["shares"] * curr["avg_cost"]) + (shares * price)
                    new_shares = curr["shares"] + shares
                    curr["avg_cost"] = round(total_cost / new_shares, 2) if new_shares > 0 else 0
                    curr["shares"] = new_shares
                    logs.append(f"✅ 買入 {curr['name']} {shares}股")
                elif action in ["賣出", "Sell"]:
                    curr["shares"] = max(0, curr["shares"] - shares)
                    logs.append(f"✅ 賣出 {curr['name']} {shares}股")
                if price > 0: curr["current_price"] = price
                is_updated = True
                continue

            # 3. 觀察清單
            match_watch = re.search(r"(關注|移除|Watch|Remove)\s+(\S+)", text, re.IGNORECASE)
            if match_watch:
                action, name = match_watch.groups()
                if name.lower() in ["rss", "新聞"]: continue 
                code = get_stock_code(name, aliases)
                
                if action in ["關注", "Watch"]:
                    market = "US" if re.match(r"^[A-Z=]+$", code) else "TW"
                    watchlist[code] = {"name": name, "market": market}
                    logs.append(f"👁️ 加入觀察: {name}")
                elif action in ["移除", "Remove"]:
                    if code in watchlist:
                        del watchlist[code]
                        logs.append(f"🗑️ 移除觀察: {name}")
                is_updated = True
                continue
            
            # 4. 別名
            match_alias = re.search(r"(設定別名|SetAlias)\s+(\S+)\s+(\S+)", text, re.IGNORECASE)
            if match_alias:
                _, nickname, code = match_alias.groups()
                aliases[nickname.upper()] = code
                logs.append(f"🏷️ 別名: {nickname} -> {code}")
                is_updated = True
                continue

            # 5. RSS
            match_rss = re.search(r"(訂閱|退訂|Sub|Unsub)(\S*)\s+(http\S+)", text, re.IGNORECASE)
            if match_rss:
                action, hint, url = match_rss.groups()
                target = config["rss_finance_tw"]
                if "資安" in hint or "Sec" in hint: target = config["rss_security"]
                elif "美股" in hint or "US" in hint: target = config["rss_finance_us"]

                if action in ["訂閱", "Sub"]:
                    if url not in target:
                        target.append(url)
                        logs.append(f"📰 訂閱: {url}")
                        is_updated = True
                elif action in ["退訂", "Unsub"]:
                    for l in [config["rss_security"], config["rss_finance_tw"], config["rss_finance_us"]]:
                        if url in l: 
                            l.remove(url)
                            is_updated = True
                            logs.append(f"🗑️ 退訂: {url}")
                continue

        if is_updated:
            config["aliases"] = aliases
            pf_data["holdings"] = holdings
            pf_data["watchlist"] = watchlist
            pf_data["config"] = config
            save_portfolio(pf_data)
        
        return logs, pf_data

    except Exception as e:
        print(f"TG 錯誤: {e}")
        return [], load_portfolio()

# ================= 執行模式 (Rich Content) =================

def run_manual_guide():
    guide = """
    📜 **Aaron 的戰情室操作指南**

    💡 輸入「指南」可喚出此選單。

    🔹 **交易**: `買進 台積電 100 1000`
    🔹 **觀察**: `關注 小那` / `移除 鴻海`
    🔹 **設定**: `設定別名 老黃 NVDA`
    🔹 **新聞**: `訂閱資安 https://url`
    """
    send_telegram(guide)

def run_security_mode(config):
    """ 早上 08:00 - 資安模式 (兩段式發送) """
    print("🛡️ 執行資安簡報...")
    urls = config.get("rss_security", [])
    raw = get_rss_data(urls)
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('models/gemini-3-flash-preview')
    
    # 1. 新聞快報 (TG)
    print("   ↳ 發送新聞快報...")
    news_prompt = f"""
    你是資安情報官。請整理以下新聞為 Telegram 訊息。
    【內容】{raw}
    【格式】
    1. 🚨 **今日高危** (最嚴重的漏洞)
    2. 🌍 **全球態勢** (其他重點)
    3. (重要！) 如果有提及影片或 Demo 連結，請務必附上。
    4. 不要使用 Markdown 符號，使用 Emoji 和空行排版。
    """
    send_telegram(model.generate_content(news_prompt).text)
    
    # 2. CISSP 微課程 (TG) - 這是你要的「詳細版」
    print("   ↳ 發送 CISSP 微課程...")
    class_prompt = f"""
    你是 CISSP 教練。請根據今日新聞，挑選一個技術主題進行教學。
    【新聞】{raw}
    【任務】寫一份「TG 手機好讀版」的微課程。
    【格式嚴格要求】
    1. 標題：🎓 **今日 CISSP 微課程：(主題)**
    2. 知識點：(對應 Domain)
    3. 攻擊手法：(駭客怎麼做？請深入技術細節)
    4. 防禦手段：(架構師怎麼防？WAF規則？Patch管理？)
    5. **不要 Markdown** (不要用 ##, **)，改用 Emoji (🔹, 🔸) 當子標題。
    6. 內容要像以前一樣豐富，不要縮水。
    """
    send_telegram(model.generate_content(class_prompt).text)

    # 3. 完整存檔 (File)
    file_prompt = f"""
    你是 CISSP 教練。完整 Markdown 版日報。
    【內容】{raw}
    【要求】詳細分析地緣政治、供應鏈影響、CISSP 八大領域對應。
    """
    save_log("security_log.md", model.generate_content(file_prompt).text)

def run_morning_forecast(pf_data):
    """ 早上 08:30 - 美股預測 """
    config = pf_data.get("config", {})
    holdings = pf_data.get("holdings", {})
    watchlist = pf_data.get("watchlist", {})
    
    targets = [f"{v['name']}({c})" for c, v in holdings.items() if v['shares']>0]
    targets += [f"觀察:{v['name']}({c})" for c, v in watchlist.items()]
    urls = config.get("rss_finance_us", [])
    raw_us = get_rss_data(urls, limit=5)
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('models/gemini-3-flash-preview')
    
    prompt = f"你是華爾街操盤手。美股新聞:{raw_us}\n關注:{', '.join(targets)}\n任務:美股收盤簡報+台股開盤預測(Emoji,手機版,不要Markdown)"
    send_telegram(model.generate_content(prompt).text)

def run_finance_mode(pf_data):
    """ 晚上 18:30 - 財經結算 (兩段式發送) """
    print("💰 執行晚間財經結算...")
    logs, pf_data = process_tg_commands()
    config = pf_data.get("config", {})
    holdings = pf_data.get("holdings", {})
    watchlist = pf_data.get("watchlist", {})
    
    urls = config.get("rss_finance_tw", [])
    raw_news = get_rss_data(urls, limit=5)
    
    stock_str = ", ".join([f"{d['name']}:{d['shares']}股" for c,d in holdings.items() if d['shares']>0])
    watch_str = ", ".join([f"{d['name']}" for c,d in watchlist.items()])
    tx_str = "\n".join(logs) if logs else "無交易"
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('models/gemini-3-flash-preview')
    
    # 1. 損益與新聞 (TG)
    print("   ↳ 發送財經日報...")
    news_prompt = f"""
    你是 CFO。
    交易:{tx_str}
    持股:{stock_str}
    觀察:{watch_str}
    新聞:{raw_news}
    【任務】
    1. 確認交易狀態。
    2. 針對持股掃描新聞 (標示利多/利空)。
    3. 明日台股趨勢。
    4. **不要 Markdown**。
    """
    send_telegram(model.generate_content(news_prompt).text)
    
    # 2. K線實戰教學 (TG) - 新增這段！
    print("   ↳ 發送 K 線教學...")
    k_prompt = f"""
    你是技術分析導師。
    請從觀察清單或持股中挑選一支股票 ({stock_str} {watch_str})，進行技術分析教學。
    【任務】寫一份「TG 手機好讀版」的實戰教學。
    【格式嚴格要求】
    1. 標題：🕯️ **今日 K 線實戰：(股票名稱)**
    2. 目前型態：(均線排列？KD交叉？MACD背離？)
    3. 教學重點：(解釋這個指標怎麼看)
    4. 操作建議：(支撐位在哪？壓力位在哪？)
    5. **不要 Markdown**，用 Emoji (📈, 📉, 🔸) 排版。
    """
    send_telegram(model.generate_content(k_prompt).text)
    
    # 3. 完整存檔 (File)
    save_log("finance_log.md", model.generate_content(f"你是分析師。完整 Markdown 報告。\n持股:{stock_str}\n新聞:{raw_news}").text)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, required=True, choices=["security", "finance", "morning", "manual"])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # ================= 測試區 (DEBUG AREA) =================
    if args.debug:
        print("🔧 進入 DEBUG 測試模式...")
        # 👇【測試用 Key - 測完請刪除】👇
        GEMINI_API_KEY = "你的_API_KEY"
        TG_BOT_TOKEN = "你的_TOKEN"
        TG_CHAT_ID = "你的_CHAT_ID"
        # 👆【測試用 Key - 測完請刪除】👆

    pf_data = load_portfolio()

    if not GEMINI_API_KEY or "貼這裡" in str(GEMINI_API_KEY):
        print("❌ 錯誤：缺少 Key！請使用 --debug 模式並填入 Key。")
        exit(1)

    if args.mode == "manual":
        run_manual_guide()
    elif args.mode == "security":
        run_security_mode(pf_data.get("config", {}))
    elif args.mode == "morning":
        run_morning_forecast(pf_data)
    elif args.mode == "finance":
        run_finance_mode(pf_data)