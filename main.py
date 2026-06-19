import os
import re
import json
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3 官方標準元件
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,  # 🚀 採用背景非同步推播，0.1秒秒回 LINE，徹底根除 5秒逾時
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK 憑證元件
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

# 🎯 宣告 FastAPI 實例 (對齊 main:app)
app = FastAPI(title="記帳米粒 ｜ 你的記帳小幫手")

# ==========================================
# ⚙️ 1. 環境變數與核心客戶端初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ"

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 🚀 初始化唯一大腦：Gemini 2.5 Flash 付費版 (拿掉 Timeout，在背景好整以暇慢慢算)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 🔥 Firebase Firestore 實體檔案安全初始化 (讀取 Render Secret File 固定掛載路徑)
cred_path = "firebase-adminsdk.json"
if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"🔥 [DATABASE LOG] 成功讀取 {cred_path}，Firestore 初始化成功！")
    except Exception as e:
        db = None
        print(f"❌ [DATABASE LOG] 檔案載入失敗但跳過崩潰: {e}")
else:
    db = None
    print(f"❌ [DATABASE LOG] 嚴重錯誤：根目錄找不到 {cred_path} 檔案！")

# ==========================================
# 🛡️ 2. 商用防禦機制與強型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "蔡英文", "賴清德", "馬英九", "柯文哲", "習近平", "共產黨", "民進黨", "國民黨", "中共", "獨立", "統一", "戰爭", "軍事", "吸毒", "賭博", "情色", "開鎖", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense", description="expense: 支出, income: 收入")
    amount: int = Field(default=0, description="金額")
    item: str = Field(default="", description="項目名稱")
    category: str = Field(default="生活雜費", description="限用: 餐飲食品、交通運輸、娛樂休閒、生活雜費、服飾美容、醫療保健、薪資收入、投資理財、其他收入")
    note: str = Field(default="", description="備註")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive"] = Field(description="意圖分流")
    records: Optional[List[SingleRecord]] = Field(default_factory=list, description="收支明細陣列")
    ai_reply: Optional[str] = Field(default="", description="回應文字")

# ==========================================
# ⚡ 3. 智慧分流攔截器 (本地 Python 節流核心)
# ==========================================
def is_pure_category_and_amount(user_text: str) -> Optional[List[SingleRecord]]:
    text_clean = user_text.strip()
    if len(text_clean) > 10: return None
    chat_keywords = ["今天", "昨天", "明天", "跟", "去", "吃", "了", "哈哈", "嗨", "你好", "幫我", "我想"]
    if any(k in text_clean for k in chat_keywords): return None

    numbers_find = list(re.finditer(r'\d+', text_clean))
    if len(numbers_find) != 1: return None
        
    try:
        match = numbers_find[0]
        amount = int(match.group())
        start_pos = match.start()
        end_pos = match.end()
        
        prev_text = text_clean[:start_pos].strip()
        next_text = text_clean[end_pos:].strip()
        
        clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text)
        clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
        
        item = clean_prev if clean_prev else (clean_next if clean_next else "日常支出")
        
        category = "生活雜費"
        official_categories = ["餐飲食品", "交通運輸", "娛樂休閒", "生活雜費", "服飾美容", "醫療保健", "薪資收入", "投資理財", "其他收入"]
        for cat in official_categories:
            if cat[:2] in item or item in cat:
                category = cat
                break
                
        r_type = "income" if any(k in item for k in ["薪水", "收入", "中獎", "賺", "薪資"]) else "expense"
        if r_type == "income" and category == "生活雜費": category = "薪資收入"

        return [SingleRecord(record_type=r_type, amount=amount, item=item, category=category, note="⚡ 本地極速記帳")]
    except Exception: return None

# ==========================================
# 🤖 4. AI 大腦與【群組級】資料庫儲存邏輯 (核心升級)
# ==========================================
def analyze_with_gemini_sync(user_text: str) -> SuperRouter:
    """【大腦】Gemini 2.5 Flash 純同步強型別調用"""
    prompt = f"""
    你是一個極簡現代風格的個人財務助理「飯糰小幫手」。請分析使用者的輸入：『{user_text}』
    
    請遵守以下規則：
    1. 【主動記帳 (record)】：無論是支出還是收入，精準判斷並拆解存入 records 陣列。
    2. 【對話中提及收支 (chat_with_record)】：聊天時提到賺錢或花錢。在 ai_reply 用「極其精簡、現代溫暖」的一句話詢問是否要記帳。
    3. 【純聊天 (chat)】：不含收支的日常問候。在 ai_reply 給出高情商且極簡的回應。此時 records 請務必給空陣列 []。
    4. 【功能代號分析 (analyze)】：若使用者有『報表、查帳、分析、明細、統計』等意圖，請將 intent 歸類為 analyze。
    5. 【回應風格】：說話俐落，不長篇大論。
    """
    
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', 
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SuperRouter,
            temperature=0.3
        ),
    )
    
    if response.parsed:
        return response.parsed
    return SuperRouter(**json.loads(response.text))


def get_line_user_profile(user_id: str) -> str:
    try:
        with ApiClient(line_config) as api_client:
            return MessagingApi(api_client).get_profile(user_id).display_name
    except Exception: return "飯糰友"


def save_records_to_db_v2(target_id: str, is_group: bool, creator_id: str, records: List[SingleRecord]) -> bool:
    """🚀 升級版資料庫儲存器：自動相容個人 (users) 與群組 (groups)"""
    if db is None or not records: return False
    try:
        creator_name = get_line_user_profile(creator_id)
        
        if is_group:
            base_ref = db.collection("groups").document(target_id)
            if not base_ref.get().exists:
                base_ref.set({"group_id": target_id, "created_at": datetime.utcnow()})
        else:
            base_ref = db.collection("users").document(target_id)
            if not base_ref.get().exists:
                base_ref.set({"line_user_id": target_id, "display_name": creator_name, "created_at": datetime.utcnow()})
        
        batch = db.batch()
        for rec in records:
            if rec.amount <= 0: continue
            doc_ref = base_ref.collection("expenses").document()
            
            payload = {
                "type": rec.record_type,
                "amount": rec.amount,
                "item": rec.item,
                "category": rec.category,
                "note": rec.note,
                "timestamp": datetime.utcnow(),
                "created_by_uid": creator_id,      
                "created_by_name": creator_name    
            }
            batch.set(doc_ref, payload)
            
        batch.commit()
        print(f"🎉 [DATABASE LOG] 成功寫入 {'群組' if is_group else '個人'} 帳本！代付者: {creator_name}", flush=True)
        return True
    except Exception as e:
        print(f"💥 [DATABASE LOG] 寫入失敗: {e}", flush=True)
        return False


def get_monthly_quick_summary_v2(target_id: str, is_group: bool) -> str:
    if db is None: return "📴 資料庫維護中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        collection_path = "groups" if is_group else "users"
        
        query = db.collection(collection_path).document(target_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
        income_total = 0; expense_total = 0
        for doc in query:
            data = doc.to_dict(); amt = data.get("amount", 0)
            if data.get("type", "expense") == "income": income_total += amt
            else: expense_total += amt
            
        title = "📊 本月群組公帳速報" if is_group else "📊 本月個人極簡速報"
        return f"{title}\n📈 總收入：${income_total:,}\n📉 總支出：${expense_total:,}\n💰 淨結餘：${(income_total - expense_total):,}"
    except Exception: return "⚠️ 查詢速報暫時失敗"


# ==========================================
# 🌐 5. Webhook 入口與多執行緒背景分流調度
# ==========================================
PENDING_CONFIRMATIONS = {}

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # 🕵️ 【安全防禦攔截 A】
    if body_str and '"text":"請教導我該如何使用？"' in body_str:
        return Response(content="OK", status_code=200)
    
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: handler.handle(body_str, signature)
    except InvalidSignatureError: print("❌ LINE 簽章驗證失敗")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    
    source_type = event.source.type  
    if source_type == "group":
        target_id = event.source.group_id 
        is_group = True
    else:
        target_id = creator_id             
        is_group = False

    reply_str = ""
    
    # 【安全防禦攔截 B】敏感話題過濾
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            reply_str = "🤖 飯糰小幫手是專屬的財務助理，無法聊政治或非財務相關的話題喔！"
            try:
                with ApiClient(line_config) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(to=target_id if not is_group else creator_id, messages=[TextMessage(text=reply_str)]))
                return
            except Exception: return

    # 1. 狀態機快捷確認優先處理
    if creator_id in PENDING_CONFIRMATIONS:
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "幫我記", "yes"]:
            saved_records = PENDING_CONFIRMATIONS.pop(creator_id)
            db_success = save_records_to_db_v2(target_id, is_group, creator_id, saved_records)
            reply_str = "👌 已幫您安全記入帳本！" if db_success else "⚠️ 寫入失敗。"
        else:
            PENDING_CONFIRMATIONS.pop(creator_id, None) 
            reply_str = "❌ 已取消該筆紀錄。"
            
    else:
        # 🚀 2. 智慧分流攔截檢測 (Python 本地直出)
        local_records = is_pure_category_and_amount(user_text)
        
        if local_records:
            db_success = save_records_to_db_v2(target_id, is_group, creator_id, local_records)
            if db_success:
                creator_name = get_line_user_profile(creator_id)
                prefix = f"👥 【群組公帳】{creator_name} 幫大家" if is_group else "✅"
                lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item} ➡️ {r.category})" for r in local_records]
                reply_str = f"{prefix}記帳成功！\n" + "\n".join(lines)
            else: reply_str = "⚠️ 備份延遲。"
                
        else:
            # 🤖 未命中純記帳格式：調度 Gemini 大腦
            try:
                result = analyze_with_gemini_sync(user_text)
                
                if result.intent == "record" and result.records:
                    db_success = save_records_to_db_v2(target_id, is_group, creator_id, result.records)
                    if db_success:
                        creator_name = get_line_user_profile(creator_id)
                        prefix = f"👥 【群組公帳】{creator_name} " if is_group else "✅ "
                        lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in result.records]
                        reply_str = f"{prefix}記帳成功！\n" + "\n".join(lines)
                    else: reply_str = "⚠️ 備份延遲。"
                elif result.intent == "chat_with_record" and result.records:
                    PENDING_CONFIRMATIONS[creator_id] = result.records
                    reply_str = f"{result.ai_reply}\n\n🔍 偵測到以下花費：\n"
                    for rec in result.records:
                        reply_str += f"・[{'收入' if rec.record_type == 'income' else '支出'}] ${rec.amount} 元 的 {rec.item}\n"
                    reply_str += "\n👉 正確請回覆「好」，若錯誤請回覆任意文字取消。"
                    
                # 🛡️ 3. 報表查詢：全面導入官方安全跳轉路由 `liff.line.me` 機制
                elif result.intent == "analyze": 
                    summary_text = get_monthly_quick_summary_v2(target_id, is_group)
                    
                    if is_group:
                        # 👥 群組公帳：拼接 ?groupId= 參數，強迫呼叫 LINE 內建 In-App 安全瀏覽器
                        dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}?groupId={target_id}"
                        reply_str = f"{summary_text}\n\n🌐 群組專屬財務後台網址：\n{dashboard_url}"
                    else:
                        # 👤 個人私帳：使用官方安全短網址跳轉
                        dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}"
                        reply_str = f"{summary_text}\n\n🌐 個人專屬雲端帳本：\n{dashboard_url}"
                        
                elif result.intent == "chat" or result.intent == "sensitive": 
                    reply_str = result.ai_reply
                else: reply_str = "👌"
                    
            except Exception as e:
                print(f"❌ Gemini 大腦處理崩潰: {e}", flush=True)
                reply_str = "🤖 飯糰大腦連線稍微波動，請稍後再試。"

    # 🚀 4. 非同步強推推播
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)])
            )
    except Exception as e: print(f"❌ 推播失敗: {e}")


@app.get("/")
def health_check():
    return {"status": "healthy", "version": "v2.6 LIFF-Redirect 完全體"}
