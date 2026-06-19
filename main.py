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

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="飯糰小幫手 ｜ 一人工作室商用最終版")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 🔥 請填入你在 LINE Developers 後台看到的 LIFF ID
MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Firebase Firestore 初始化驗證
cred_path = "firebase-adminsdk.json"
if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] 成功建立 Firestore 安全連線通道！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 商用安全字典、結構與全域狀態快取
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

# 🚀 記憶快取：儲存待確認的記帳資料結構，防範 NameError 關鍵核心
PENDING_CONFIRMATIONS = {}

# 本地快速解析正則式（不耗 Token）
ACCOUNTING_PATTERN = re.compile(r'^([^\s]+)\s+(\d+)\s+([^\s]+)$')
SETTLE_PATTERN_1 = re.compile(r'^核銷\s+(\d+)\s+([^\s]+)$')
SETTLE_PATTERN_2 = re.compile(r'^([^\s]+)\s+(\d+)\s+核銷$')

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive"] = Field(description="核心意圖分流")
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 智慧分流與 LINE 連線工具組
# ==========================================
def get_line_user_profile(user_id: str) -> str:
    """透過 LINE 官方 API 逆查成員真實暱稱，避免資料庫出現未知記帳"""
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            return res.json().get("displayName", "未知成員")
    except Exception:
        pass
    return "群組夥伴"

def is_pure_category_and_amount(user_text: str) -> Optional[List[SingleRecord]]:
    """極速判斷：若純打「便當 120」，免經大腦直接落庫"""
    text_clean = user_text.strip()
    if len(text_clean) > 10 or any(k in text_clean for k in ["今天", "昨天", "了", "哈哈", "嗨", "核銷"]): 
        return None
    numbers_find = list(re.finditer(r'\d+', text_clean))
    if len(numbers_find) != 1: 
        return None
    try:
        match = numbers_find[0]
        amount = int(match.group())
        prev_text = text_clean[:match.start()].strip()
        next_text = text_clean[match.end():].strip()
        clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text)
        clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
        item = clean_prev if clean_prev else (clean_next if clean_next else "日常支出")
        
        category = "生活雜費"
        official_categories = ["餐飲食品", "交通運輸", "娛樂休閒", "生活雜費", "服飾美容", "醫療保健", "薪資收入", "投資理財", "其他收入"]
        for cat in official_categories:
            if cat[:2] in item or item in cat: 
                category = cat
                break
        r_type = "income" if any(k in item for k in ["薪水", "收入", "賺"]) else "expense"
        return [SingleRecord(record_type=r_type, amount=amount, item=item, category=category, note="⚡ 本地極速")]
    except Exception: 
        return None

def process_settlement_local(group_id: str, creator_id: str, user_text: str) -> Optional[str]:
    """🎯 群組核銷解析器：將成員平帳資訊記入資料庫，若不合格式則回傳 None 保底"""
    text_clean = user_text.strip()
    match1 = SETTLE_PATTERN_1.match(text_clean)
    match2 = SETTLE_PATTERN_2.match(text_clean)
    
    amount, target_name = None, None
    if match1:
        amount = int(match1.group(1))
        target_name = match1.group(2)
    elif match2:
        target_name = match2.group(1)
        amount = int(match2.group(2))
        
    if amount and target_name:
        try:
            payer_name = get_line_user_profile(creator_id)
            group_ref = db.collection("groups").document(group_id)
            if not group_ref.get().exists: 
                group_ref.set({"group_id": group_id, "created_at": datetime.utcnow()})
            
            group_ref.collection("settlements").document().set({
                "payer_uid": creator_id,
                "payer_name": payer_name,
                "receiver_name": target_name,
                "amount": amount,
                "timestamp": datetime.utcnow()
            })
            return f"🤝 【群組核銷成功】\n💸 付款對帳者：{payer_name}\n📥 收款對象：{target_name}\n💰 核銷金額：${amount:,} 元\n\n系統已自動完成對帳登記，詳細流水請點開後台「核銷紀錄」查看！"
        except Exception as e:
            return f"⚠️ 核銷寫入失敗，請檢查資料庫連線: {e}"
    return None

# ==========================================
# 🤖 4. AI 核心運算與資料庫同步控制
# ==========================================
def analyze_with_gemini_sync(user_text: str) -> SuperRouter:
    prompt = f"你是一個具備頂級商業敏銳度的記帳助理「飯糰小幫手」。請透視使用者的語意輸入：『{user_text}』進行強型別分流。若提到報表、查帳、統計、看後台等，intent 務必歸為 analyze。"
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.2),
    )
    if response.parsed: 
        return response.parsed
    return SuperRouter(**json.loads(response.text))

def save_records_to_db_v2(target_id: str, is_group: bool, creator_id: str, records: List[SingleRecord]) -> bool:
    if db is None or not records: 
        return False
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
            if rec.amount <= 0: 
                continue
            doc_ref = base_ref.collection("expenses").document()
            batch.set(doc_ref, {
                "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category, "note": rec.note,
                "timestamp": datetime.utcnow(), "created_by_uid": creator_id, "created_by_name": creator_name
            })
        batch.commit()
        return True
    except Exception: 
        return False

def get_monthly_quick_summary_v2(target_id: str, is_group: bool) -> str:
    if db is None: 
        return "📴 資料庫系統維護中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        if is_group:
            exp_query = db.collection("groups").document(target_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
            settle_query = db.collection("groups").document(target_id).collection("settlements").where("timestamp", ">=", start_of_month).stream()
            expense_total = sum(doc.to_dict().get("amount", 0) for doc in exp_query if doc.to_dict().get("type") == "expense")
            settle_count = len(list(settle_query))
            return f"👥 【本月群組帳務速報】\n📉 當月公帳總開銷：${expense_total:,}\n🤝 已登錄核銷筆數：{settle_count} 筆"
        else:
            query = db.collection("users").document(target_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
            income_total = 0; expense_total = 0
            for doc in query:
                data = doc.to_dict(); amt = data.get("amount", 0)
                if data.get("type", "expense") == "income": 
                    income_total += amt
                else: 
                    expense_total += amt
            return f"📊 【本月個人財務速報】\n📈 當月總收入：${income_total:,}\n📉 當月總支出：${expense_total:,}\n💰 錢包淨結餘：${(income_total - expense_total):,}"
    except Exception: 
        return "⚠️ 讀取當月資料庫速報時發生延遲"

# ==========================================
# 🌐 5. Webhook 入口與多執行緒異步調度
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: 
        raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # 解決 LINE 官方 LIFF 自動觸發機制，防範空響應
    if body_str and '"text":"請教導我該如何使用？"' in body_str: 
        return Response(content="OK", status_code=200)
        
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: 
        handler.handle(body_str, signature)
    except InvalidSignatureError: 
        print("❌ [SIGNATURE ERROR] LINE Webhook 簽章密鑰驗證失敗！", flush=True)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    target_id = event.source.group_id if is_group else creator_id

    reply_str = ""

    # 🛑 攔截機制一層：敏感詞防禦
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            reply_str = "🤖 飯糰小幫手為純財務系統，無法回應與處理敏感話題喔！"
            try:
                with ApiClient(line_config) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)]))
                return
            except Exception: 
                return

    # 🤝 攔截機制二層：群組核銷指令本地高效率匹配
    if is_group and ("核銷" in user_text):
        settle_reply = process_settlement_local(target_id, creator_id, user_text)
        if settle_reply:
            try:
                with ApiClient(line_config) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=settle_reply)]))
                return
            except Exception: 
                return

    # 🔄 攔截機制三層：確認狀態機
    if creator_id in PENDING_CONFIRMATIONS:
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "yes", "OK"]:
            saved_records = PENDING_CONFIRMATIONS.pop(creator_id)
            db_success = save_records_to_db_v2(target_id, is_group, creator_id, saved_records)
            reply_str = "👌 收到！已成功幫您記錄這筆花費至雲端！" if db_success else "⚠️ 雲端備份稍微延遲，請稍後檢查。"
        else:
            PENDING_CONFIRMATIONS.pop(creator_id, None)
            reply_str = "❌ 已取消該筆紀錄，大腦將不記入帳本。"
    else:
        # 常規管道分流：本地快速檢測
        local_records = is_pure_category_and_amount(user_text)
        if local_records:
            db_success = save_records_to_db_v2(target_id, is_group, creator_id, local_records)
            if db_success:
                creator_name = get_line_user_profile(creator_id)
                prefix = f"👥 【群組公帳】{creator_name} 幫大家" if is_group else "✅ "
                lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in local_records]
                reply_str = f"{prefix}記帳成功！\n" + "\n".join(lines)
            else: 
                reply_str = "⚠️ 寫入失敗，請檢查網路連線。"
        else:
            # 啟動 Gemini 運算大腦
            try:
                result = analyze_with_gemini_sync(user_text)
                if result.intent == "record" and result.records:
                    db_success = save_records_to_db_v2(target_id, is_group, creator_id, result.records)
                    if db_success:
                        creator_name = get_line_user_profile(creator_id)
                        prefix = f"👥 【群組公帳】{creator_name} " if is_group else "✅ "
                        lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in result.records]
                        reply_str = f"{prefix}記帳成功！\n" + "\n".join(lines)
                    else: 
                        reply_str = "⚠️ 寫入資料庫時因格式不符延遲。"
                elif result.intent == "chat_with_record" and result.records:
                    PENDING_CONFIRMATIONS[creator_id] = result.records
                    reply_str = f"{result.ai_reply}\n\n🔍 飯糰發現記帳意圖，請回覆「好」確認幫您落庫。"
                elif result.intent == "analyze":
                    summary_text = get_monthly_quick_summary_v2(target_id, is_group)
                    dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}?groupId={target_id}" if is_group else f"https://liff.line.me/{MY_LIFF_ID}"
                    reply_str = f"{summary_text}\n\n🌐 雲端可視化大後台：\n{dashboard_url}"
                elif result.intent == "chat" or result.intent == "sensitive":
                    reply_str = result.ai_reply
                else: 
                    reply_str = ""
            except Exception as e:
                print(f"Gemini 連線異常: {e}")
                reply_str = "🤖 飯糰小幫手大腦思考稍微超時，請您再試一次！"

    # 🛡️ Pydantic 強型別防禦：杜絕 TextMessage none 報錯
    if not reply_str or reply_str.strip() == "":
        reply_str = "🤖 飯糰小幫手收到！若想記帳，請輸入如「早餐 65 餐飲」；若想群組平帳，請輸入「核銷 500 阿誠」喔！"

    # 推播發送
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=reply_str)]))
    except Exception as e: 
        print(f"❌ [PUSH ERROR] LINE 訊息發送失敗: {e}", flush=True)

@app.get("/")
def health_check(): 
    return {"status": "healthy", "version": "v3.0-OneManStudio-Final"}
