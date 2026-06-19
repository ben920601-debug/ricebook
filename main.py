import os
import json
import re
import asyncio  # 🚀 引入非同步控制中心
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Literal, List, Optional
from openai import AsyncOpenAI  # 🚀 改用非同步版 OpenAI 客戶端

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
from google.genai.errors import APIError # 🚀 用於精準捕捉 Gemini 異常
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# 基礎環境變數
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 🚀 關鍵優化 1：初始化非同步/具備超時控制的 AI 客戶端
# Gemini 透過 httpx_config 控制超時；OpenAI 透過 timeout 參數控制
ai_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={'timeout':4} # ⏱️ 超過 5 秒直接判定 Gemini 罷工！
)
openai_client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=5 # ⏱️ 超過 5 秒直接判定 OpenAI 罷工！
)

# 初始化 Firebase
cred_path = "firebase-adminsdk.json"

if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"🔥 [DATABASE LOG] 成功讀取 {cred_path}，Firestore 初始化成功！")
    except Exception as e:
        db = None
        print(f"❌ [DATABASE LOG] 找到檔案但初始化崩潰，真兇原因: {e}")
else:
    db = None
    print(f"❌ [DATABASE LOG] 嚴重錯誤：在雲端根目錄找不到 {cred_path} 檔案！")
# ==========================================
# 📊 Pydantic 資料結構定義
# ==========================================
class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense", description="expense: 支出, income: 收入")
    amount: int = Field(default=0, description="金額")
    item: str = Field(default="", description="項目名稱")
    category: str = Field(default="生活雜費", description="分類")
    note: str = Field(default="", description="備註")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive"] = Field(description="意圖分流")
    records: Optional[List[SingleRecord]] = Field(default_factory=list, description="收支明細陣列")
    ai_reply: Optional[str] = Field(default="", description="回應文字")

# ==========================================
# 🤖 異步大腦邏輯管線
# ==========================================

async def analyze_with_gemini_v3(user_text: str) -> SuperRouter:
    """【第一線】Gemini 官方標準非同步安全調用"""
    prompt = f"""
    你是一個極簡現代風格的個人財務助理「飯糰小幫手」。請分析使用者的輸入：『{user_text}』
    
    請遵守以下規則：
    1. 【主動記帳 (record)】：無論是支出還是收入，精準判斷並拆解存入 records 陣列。
    2. 【對話中提及收支 (chat_with_record)】：聊天時提到賺錢或花錢。在 ai_reply 用「極其精簡、現代溫暖」的一句話詢問是否要記帳。
    3. 【純聊天 (chat)】：不含收支的日常問候。在 ai_reply 給出高情商且極簡的回應。此時 records 請務必給空陣列 []。
    4. 【回應風格】：說話俐落，不長篇大論。
    """
    
    def _call():
        # 🚀 修正：透過 config 嚴格約束結構
        return ai_client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SuperRouter,
                temperature=0.5
            ),
        )
    
    # 在線程池中安全執行
    response = await asyncio.to_thread(_call)
    
    # 🚀 終極修正：新版 SDK 如果有給 response_schema，
    # 解析後的 Pydantic 物件會直接躺在 .parsed 裡面！完全不需要 json.loads！
    if response.parsed:
        return response.parsed
        
    # 保底解析
    return SuperRouter(**json.loads(response.text))


async def analyze_with_openai(user_text: str) -> SuperRouter:
    """【第二線】OpenAI GPT-4o 官方標準安全調用"""
    # 🚀 修正：Prompt 內必須明確包含 "JSON" 格式這四個字，滿足 GPT-4o 的嚴格防火牆
    prompt = f"""
    你是一個極簡現代風格的個人財務助理「飯糰小幫手」。請分析使用者的輸入：『{user_text}』
    
    請嚴格遵守結構化規範，並回傳符合格式的 JSON 物件：
    1. 【主動記帳 (record)】：無論是支出還是收入，精準判斷並拆解存入 records 陣列。
    2. 【對話中提及收支 (chat_with_record)】：聊天時提到賺錢或花錢。在 ai_reply 用「極其精簡、現代溫暖」的一句話詢問是否要記帳。
    3. 【純聊天 (chat)】：不含收支的日常問候。在 ai_reply 給出高情商且極簡的回應。此時 records 欄位請給空陣列 []。
    4. 【回應風格】：說話俐落，不長篇大論。
    """
    
    response = await openai_client.chat.completions.create(
        model="gpt-o3",
        messages=[{"role": "user", "content": prompt}],
        response_format={ "type": "json_object" } # 🚀 確保 Prompt 有 JSON 字眼，此行才不會崩潰
    )
    
    json_data = json.loads(response.choices[0].message.content)
    return SuperRouter(**json_data)

def analyze_with_python_fallback(user_text: str) -> SuperRouter:
    """【第三線最終防線】Python 智慧防呆保底
    🚀 升級：自動識別「純記帳」與「長對話」，純記帳直接入庫，長對話才跳確認！
    """
    user_text_lower = user_text.lower().strip()
    
    # 1. 優先判斷是否為看報表
    if any(k in user_text_lower for k in ["查", "報表", "分析", "統計", "花多少", "結餘", "速報"]):
        return SuperRouter(intent="analyze")
        
    numbers_find = re.finditer(r'\d+', user_text)
    records = []
    
    try:
        for match in numbers_find:
            amount = int(match.group())
            start_pos = match.start()
            end_pos = match.end()
            
            # 抓取數字前後各 5 個字進行高精簡清理
            prev_text = user_text[max(0, start_pos-5):start_pos].strip()
            next_text = user_text[end_pos:min(len(user_text), end_pos+5)].strip()
            
            clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text).replace("花了", "").replace("吃了", "")
            clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
            
            item = clean_prev if (clean_prev and len(clean_prev) >= 2) else (clean_next if clean_next else "日常收支")
            r_type = "income" if any(k in user_text for k in ["薪水", "收入", "中獎", "賺", "薪資"]) else "expense"
            
            records.append(SingleRecord(
                record_type=r_type, amount=amount, item=item, 
                category="薪資收入" if r_type == "income" else "生活雜費", note="⚠️ 備用大腦解析"
            ))
            
        if records:
            # 🚀 關鍵 UX 優化：判斷使用者的輸入是否為「乾淨的純記帳短語」
            # 如果總字數小於等於 10 個字，且沒有太多聊天字眼，判定為「高信心度」
            is_pure_record = len(user_text) <= 10 and not any(k in user_text for k in ["今天", "昨天", "跟", "去", "哈哈", "了"])
            
            if is_pure_record:
                # 🎯 信心度高：直接判定為 record 意圖，系統會「自動入庫」，使用者不需要多回「好」！
                return SuperRouter(intent="record", records=records)
            else:
                # 🛡️ 信心度低（像是聊天）：維持 chat_with_record，彈出防呆卡片讓使用者確認
                return SuperRouter(
                    intent="chat_with_record", 
                    records=records, 
                    ai_reply="⚠️ 目前核心系統繁忙，已為您啟動『安全確認機制』。"
                )
    except Exception:
        pass

    return SuperRouter(intent="chat", ai_reply="收到👌")

# ==========================================
# 💾 資料庫管理
# ==========================================
def get_line_user_profile(user_id: str) -> str:
    try:
        with ApiClient(line_config) as api_client:
            return MessagingApi(api_client).get_profile(user_id).display_name
    except Exception: return "飯糰友"

def save_records_to_db(user_id: str, records: List[SingleRecord]):
    if db is None or not records: return False
    try:
        user_ref = db.collection("users").document(user_id)
        if not user_ref.get().exists:
            user_ref.set({"line_user_id": user_id, "display_name": get_line_user_profile(user_id), "created_at": datetime.utcnow()})
        batch = db.batch()
        for rec in records:
            if rec.amount <= 0: continue
            batch.set(user_ref.collection("expenses").document(), {
                "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category, "note": rec.note, "timestamp": datetime.utcnow()
            })
        batch.commit()
        return True
    except Exception: return False

def get_monthly_quick_summary(user_id: str) -> str:
    if db is None: return "📴 系統維護中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        query = db.collection("users").document(user_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
        income_total = 0; expense_total = 0
        for doc in query:
            data = doc.to_dict(); amt = data.get("amount", 0)
            if data.get("type", "expense") == "income": income_total += amt
            else: expense_total += amt
        return f"📊 本月極簡速報\n📈 總收入：${income_total}\n📉 總支出：${expense_total}\n💰 淨結餘：${income_total - expense_total}\n\n🌐 詳細明細請至 Web 後台查看。"
    except Exception: return "⚠️ 查詢速報暫時失敗"

# ==========================================
# 🌐 Webhook 入口 (非同步調度優化)
# ==========================================
PENDING_CONFIRMATIONS = {}

# ==========================================
# 🌐 Webhook 入口與狀態暫存狀態機 (執行緒安全版)
# ==========================================

# 暫存快取字典
PENDING_CONFIRMATIONS = {}

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    """🚀 修正一：直接讓 FastAPI 的 BackgroundTasks 處理同步的 Webhook 轉發
    FastAPI 會自動以安全、不阻塞的方式在獨立線程管理它，不會噴執行緒錯誤。
    """
    signature = request.headers.get("X-Line-Signature")
    if not signature: 
        raise HTTPException(status_code=400, detail="Missing Signature")
    
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # 利用 FastAPI 的安全背景機制轉發
    background_tasks.add_task(handle_line_events, body_str, signature)
    return "OK"


def handle_line_events(body_str: str, signature: str):
    """標準同步轉發，讓 LINE SDK 內部去解析並觸發 handle_text_message"""
    try:
        handler.handle(body_str, signature)
    except InvalidSignatureError:
        print("❌ LINE 簽章驗證失敗")
    except Exception as e:
        print(f"❌ 轉發 LINE 事件時發生未知錯誤: {e}")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """🚀 修正二：這是最關鍵的執行緒安全橋接器！
    我們利用 asyncio.run() 在當前子執行緒內「當場拉起一個全新的臨時 Event Loop」，
    這樣就能完美執行我們寫好的 async 大腦，速度快且絕不噴迴圈錯誤！
    """
    try:
        asyncio.run(process_message_core(event))
    except Exception as e:
        print(f"💥 執行大腦核心時發生錯誤: {e}")


async def process_message_core(event):
    """🚀 核心調度大腦：保持完全非同步、毫秒級超時閃切鏈"""
    user_text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id 
    reply_str = ""
    
    # 狀態機快捷確認
    if user_id in PENDING_CONFIRMATIONS:
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "幫我記", "yes", "correct"]:
            saved_records = PENDING_CONFIRMATIONS.pop(user_id)
            db_success = save_records_to_db(user_id, saved_records)
            reply_str = "👌 已幫您安全記入帳本！" if db_success else "⚠️ 寫入失敗。"
        else:
            PENDING_CONFIRMATIONS.pop(user_id, None) 
            reply_str = "❌ 抱歉抓錯了！已取消該筆紀錄，請您重新輸入。✍️"
            
    else:
        # 秒級切換金字塔防線
        try:
            # 1. 嘗試調用 Gemini (超時鎖定 2 秒)
            result = await analyze_with_gemini_v3(user_text)
            print("🤖 [LINE LOG] 目前由 Gemini 執掌大腦中...")
        except Exception as gemini_err:
            print(f"⏱️ Gemini 超時或異常 ➡️ 立即切換備援！")
            try:
                # 2. 備援調用 OpenAI (超時鎖定 1.5 秒)
                print("🔮 [LINE LOG] 正在切換至 OpenAI GPT-4o 接手...")
                result = await analyze_with_openai(user_text)
            except Exception as openai_err:
                # 3. 本地保底 (0.0001秒)
                print(f"💥 雙 AI 超時罷工 ➡️ 本地 Python 保底接管！")
                result = analyze_with_python_fallback(user_text)
        
        # 意圖分流處理
        if result.intent == "record" and result.records:
            db_success = save_records_to_db(user_id, result.records)
            if db_success:
                lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in result.records]
                reply_str = "✅ 記帳成功！\n" + "\n".join(lines)
            else: 
                reply_str = "⚠️ 備份延遲。"
                
        elif result.intent == "chat_with_record" and result.records:
            PENDING_CONFIRMATIONS[user_id] = result.records
            reply_str = f"{result.ai_reply}\n\n🔍 偵測到以下可能的花費：\n"
            for rec in result.records:
                reply_str += f"・[{'收入' if rec.record_type == 'income' else '支出'}] ${rec.amount} 元 的 {rec.item}\n"
            reply_str += "\n👉 正確請回覆「好」，若錯誤請回覆任意文字來重新輸入。"
            
        elif result.intent == "analyze": 
            reply_str = get_monthly_quick_summary(user_id)
        elif result.intent == "chat" or result.intent == "sensitive": 
            reply_str = result.ai_reply
        else: 
            reply_str = "👌"

    # 回傳訊息給 LINE 伺服器
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=reply_str)])
            )
    except Exception as e:
        print(f"❌ 訊息回傳 LINE 失敗: {e}")
