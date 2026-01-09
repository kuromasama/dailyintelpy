import google.generativeai as genai
import os

# 填入你的 API Key
GEMINI_API_KEY = "AIzaSyC3wiAuM5HFdsqs3PaGds_bBTMnFIH457U"
genai.configure(api_key=GEMINI_API_KEY)

print("📋 你的帳號目前可用的模型有：")
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(f" - {m.name}")

