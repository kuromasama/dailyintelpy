import google.generativeai as genai
import os

# 填入你的 API Key
GEMINI_API_KEY = ""
genai.configure(api_key=GEMINI_API_KEY)

print("📋 你的帳號目前可用的模型有：")
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(f" - {m.name}")

# 步驟 1：把你的「本地修改」先暫存起來 (藏到抽屜裡)
#git stash

# 步驟 2：把雲端的最新版拉下來 (現在抽屜清空了，可以安全下載)
#git pull origin main

# 步驟 3：(選擇性) 如果你不需要剛剛手動改的內容，這步可以不做。
# 如果你想把你剛剛改的東西「合併」回來，請執行：
# git stash pop

# 加入新的 Python 主程式 和 Workflow 設定檔
#git add Daily_Cyber_Fin_v0.0.1.py .github/workflows/
#git add Daily_Cyber_Fin_v0.0.1.py .github/workflows/ finance_log_v2.md security_log_v2.md
#git commit -m "Deploy v0.0.1 system & upload V2 historical logs"