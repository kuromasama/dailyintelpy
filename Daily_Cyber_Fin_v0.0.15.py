import feedparser
import google.generativeai as genai
import requests
import os
import argparse
import json
import re
import time
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
SECURITY_TG_LOG = "security_tg.log"
FINANCE_TG_LOG = "finance_tg.log"

# ================= 模型配置 =================
MODEL_NAME = 'models/gemini-3-flash-preview'

# 關閉安全濾網
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

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

def send_telegram(token, message, log_file=None):
    if not token: print(f"[模擬發送] {message[:50]}..."); return
    if not message: return 
    
    clean_message = message.replace("**", "").replace("##", "").replace("###", "").replace("__", "").replace("`", "")
    clean_message = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", clean_message)

    if log_file:
        try:
            # TG Log 依然採用 Append (保留時間順序方便 Debug)
            if not os.path.exists(log_file):
                with open(log_file, 'w', encoding='utf-8') as f: pass
            timestamp = get_tw_time().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*30}\n[{timestamp}] SENT:\n{clean_message}\n{'='*30}\n")
        except Exception as e: print(f"❌ Log 寫入失敗: {e}")

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
    now_utc = datetime.utcnow()
    
    if not urls: return "無訂閱來源"
    
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if len(processed) >= limit: break
                if entry.title in processed: continue
                if history_content and (entry.link in history_content or entry.title in history_content):
                    continue 

                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_time_utc = datetime(*entry.published_parsed[:6])
                    if (now_utc - pub_time_utc).total_seconds() > hours_limit * 3600: continue
                
                processed.append(entry.title)
                buffer.append(f"標題: {entry.title}\n連結: {entry.link}\n")
        except: continue
    return "\n".join(buffer) if buffer else ""

# ✅ 【修正 1】MD Log 改為 Prepend (置頂寫入)
def save_log(filename, content):
    if not content: return
    try:
        old_content = ""
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                old_content = f.read()
        
        # 新內容在最上面 + 分隔線 + 舊內容
        new_full_content = f"{content}\n\n{'='*50}\n\n{old_content}"
        
        with open(filename, "w", encoding="utf-8") as f:
            f.write(new_full_content)
        print(f"💾 已存檔至 {filename} (置頂模式)")
    except Exception as e:
        print(f"❌ 存檔失敗: {e}")

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

# ✅ 【修正 2】智慧重試生成函數 (解決 429 和 finish_reason 1)
def generate_safe_content(model, prompt, retries=3):
    for attempt in range(retries):
        try:
            # 加入 safety_settings 防止被擋
            response = model.generate_content(prompt, safety_settings=SAFETY_SETTINGS)
            if response.text:
                return response.text
            else:
                print(f"⚠️ [Attempt {attempt+1}] 生成為空，重試中...")
        except Exception as e:
            error_str = str(e)
            print(f"⚠️ [Attempt {attempt+1}] API 錯誤: {error_str}")
            
            # 如果是 429 (Quota exceeded)，強制冷卻長一點
            if "429" in error_str or "Quota exceeded" in error_str:
                wait_time = 35 # 建議休息 30 秒以上
                print(f"⏳ 觸發速率限制，休息 {wait_time} 秒...")
                time.sleep(wait_time)
            else:
                # 其他錯誤 (如網路不穩)，休息短一點
                time.sleep(5)
    
    return "⚠️ 內容生成失敗 (已達重試上限)。"

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

# ================= 執行模式 (V0.0.14 - 穩壓置頂版) =================

def run_security_mode(config):
    """ 資安 Bot """
    token = TG_BOT_TOKEN_SEC
    print(f"🛡️ [資安 Bot] 啟動... ({MODEL_NAME})")
    today_str = get_tw_time().strftime("%Y/%m/%d")
    
    history = read_history_log(SECURITY_LOG_FILE)
    time_limit = 168 if len(history) < 100 else 24
    
    urls = config.get("rss_security", [])
    raw = get_rss_data(urls, limit=10, hours_limit=time_limit, history_content=history)
    
    if not raw:
        print("✅ 無新進重要新聞")
        return

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(MODEL_NAME)
    
    # 1. 新聞快報
    print(" ↳ 發送新聞快報 (TG)...")
    news_prompt = f"""
    你是資安情報官。請整理以下 RSS 情報。
    【內容】{raw}
    【格式要求】
    1. **標題必須翻譯成流暢的繁體中文** (不要顯示英文)。
    2. 使用 **生動的 Emoji** (如 🚨, 🛡️, ⚠️) 放在標題前。
    3. 格式：
       🔥 **中文標題**
       📅 事件背景：(一句話解釋)
       🔗 連結
       📝 摘要：(1-2 句重點補充)
    4. 嚴禁 Markdown 粗體。
    """
    send_telegram(token, generate_safe_content(model, news_prompt), log_file=SECURITY_TG_LOG)
    
    # ✅ 冷卻 15 秒
    print("⏳ 冷卻中...") 
    time.sleep(15)
    
    # 2. CISSP 微課程
    print(" ↳ 發送 CISSP 多則教學 (TG)...")
    
    selection_prompt = f"""
    請從以下新聞中，挑選 **2 則** 最具教學價值的技術新聞。
    回傳標題即可。
    【新聞】{raw}
    """
    selected_titles = generate_safe_content(model, selection_prompt)
    
    if "⚠️" not in selected_titles:
        class_prompt = f"""
        你是 CISSP 資深教練。請針對這 2 則新聞：
        {selected_titles}
        
        撰寫兩段獨立的「深度技術教學」。
        
        【每一則的格式要求】
        -------------------------
        🎓 **CISSP 實戰：(新聞標題)**
        
        📚 **案例故事**：(用「想像你是...」的口吻，生動描述情境)
        
        🧠 **核心名詞解釋**：(挑選 2 個專有名詞，用**白話文比喻**詳細解釋，例如：供應鏈攻擊就像是超市鮮奶被下毒)
        
        ⚔️ **紅隊技術解構**：(條列式 1,2,3，說明攻擊原理)
        
        🛡️ **藍隊防禦策略**：(條列式 1,2,3，說明防禦手段)
        -------------------------
        
        嚴禁 Markdown 粗體。
        """
        send_telegram(token, generate_safe_content(model, class_prompt), log_file=SECURITY_TG_LOG)
    else:
        print("❌ 跳過教學生成，因為選題失敗")
    
    # ✅ 冷卻 15 秒
    print("⏳ 冷卻中...") 
    time.sleep(15)

    # 3. MD 日報
    print(" ↳ 生成詳細 MD 日報 (For NotebookLM)...")
    file_prompt = f"""
    請撰寫一份 **資安戰情白皮書 (Markdown 格式)**，此文件將用於 AI 知識庫 (NotebookLM) 訓練。
    標題：# 🛡️ 資安戰情白皮書 ({today_str})
    【內容】{raw}
    
    【要求】
    1. **生動專業**：保留 Emoji，語氣詳盡。
    2. **資訊密度高**：不要簡寫，要盡量展開技術細節。
    
    【結構】
    ## 1. 👨‍💼 CISO 架構師總結
    - 威脅態勢與戰略建議。
    
    ## 2. 🌍 全球威脅深度列表
    - 列出所有新聞 (中英對照)。
    
    ## 3. 🎯 全面技術攻防演練
    - **針對「每一則」新聞，撰寫獨立分析**。
    - 包含：**🔍 技術原理**、**⚔️ 攻擊向量**、**🛡️ 防禦緩解**、**🧠 名詞定義**。
    
    ## 4. 🔮 威脅趨勢與未來預測
    - 預測未來變種攻擊。
    
    ## 5. 🔗 參考文獻
    - 附上所有原始連結。
    """
    save_log(SECURITY_LOG_FILE, generate_safe_content(model, file_prompt))

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
    send_telegram(token, generate_safe_content(model, prompt), log_file=FINANCE_TG_LOG)

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
    
    # 變數初始化
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
    
    # 1. 持股戰情牆
    if tech_lines:
        clean_tech = tech_str.replace("**", "") 
        status = "🟢" if market_open else "🔴"
        clean_summary = summary_line.replace("**", "")
        msg = f"📊 **持股戰情牆** ({status})\n\n{clean_summary}\n\n{clean_tech}\n\n📝 **系統**: {' '.join(logs) if logs else '無交易'}"
        send_telegram(token, msg, log_file=FINANCE_TG_LOG)
    
    # ✅ 冷卻 10 秒
    print("⏳ 冷卻中...") 
    time.sleep(10)
    
# 2. 策略分析與教學 (Prompt 大升級)
    print(" ↳ 發送新聞與教學...")
    strategy_prompt = f"""
    你是華爾街資深操盤手與技術分析導師。
    
    【使用者持股狀態】
    {tech_str}
    
    【今日市場新聞】
    {raw_news}
    
    請完成以下兩項任務：

    ### 任務 1: 持股深度診斷 (針對上述持股中，波動最大或最重要的 2 檔)
    請針對這兩檔股票，給出專業的操盤建議：
    
    🔹 **股票 A：(股名)**
    * **走勢判讀**：(一句話，例如：突破均線，強勢表態)
    * **🎯 技術目標價**：(請根據趨勢給出一個預估價位或區間)
    * **🛡️ 支撐/壓力**：(給出下檔支撐與上檔壓力的具體價格)
    * **💡 操作建議**：(加碼 / 續抱 / 減碼 / 停損)

    🔹 **股票 B：(股名)**
    * **走勢判讀**：
    * **🎯 技術目標價**：
    * **🛡️ 支撐/壓力**：
    * **💡 操作建議**：

    ### 任務 2: 實戰 K 線教學
    觀察今日盤勢，挑選一個值得教學的技術型態（例如：跳空缺口、十字線、多頭排列）：
    * 🎓 **型態名稱**：
    * 🧠 **白話文解釋**：(這代表主力在想什麼？)
    * ✨ **下一步預測**：

    (要求：嚴禁 Markdown 粗體，請使用 Emoji 排版，語氣專業且犀利)
    """
    
    send_telegram(token, generate_safe_content(model, strategy_prompt), log_file=FINANCE_TG_LOG)
    
    # ✅ 冷卻 10 秒
    print("⏳ 冷卻中...") 
    time.sleep(10)
    
    # 3. MD 日報
    if mode == "finance":
        print(" ↳ 生成詳細 MD 日報...")
        file_prompt = f"""
        請撰寫一份 **投資策略白皮書 (Markdown 格式)**，此文件將用於知識庫訓練，內容須詳盡。
        標題：# 💰 投資戰情日報 ({today_str})
        【持股數據】{tech_str}
        【市場新聞】{raw_news}
        
        【要求】
        1. **生動專業**：保留 Emoji，風格像專業交易員筆記。
        2. **數據佐證**：分析時多引用數據。
        
        【結構】
        ## 1. 📈 持股深度診斷
        - **請使用 Markdown 表格** 列出持股狀態。
        - 針對重點持股，進行技術面與基本面雙重分析。
        
        ## 2. 🕯️ K 線技術教學 (含圖解)
        - **請用 ASCII Art 畫出今日 K 線**。
        - 詳細解釋技術指標含義。
        
        ## 3. 📝 交易員心得筆記
        - 總結今日盤勢與明日重點觀察股。
        """
        save_log(FINANCE_LOG_FILE, generate_safe_content(model, file_prompt))

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
