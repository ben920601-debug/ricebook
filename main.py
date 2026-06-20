import os
import re
import json
import random
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

app = FastAPI(title="記帳米粒 ｜ 隨身攜帶的小帳本")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] Firestore 連線就位！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(default="")
    receiver_name: str = Field(default="")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(default="")
    item_name: str = Field(default="")
    price: int = Field(default=0)

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "sensitive", "settlement", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    target_payer: Optional[str] = Field(default="")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def send_line_reply(target_id: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
    except Exception as e:
        print(f"❌ LINE 推播失敗: {e}", flush=True)

def fetch_line_profile_name(user_id: str) -> str:
    """🌐 透過 LINE 官方 API 逆查成員真實暱稱 (保底防線)"""
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            return res.json().get("displayName", f"成員({user_id[:4]})")
    except Exception:
        pass
    return f"成員({user_id[:4]})"

def resolve_id_to_name(target_id: str, user_id: str) -> str:
    """👥 智慧名冊反查工具：本地快取優先，若無則請求官方 API 並自動回寫快取"""
    if not db or not user_id: return "群組夥伴"
    if not user_id.startswith("U"): return user_id
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", f"成員({user_id[:4]})")
        else:
            # 🚀 本地名冊找不到，主動跟 LINE 要，並且回寫到快取池，下次就不重複呼叫
            real_name = fetch_line_profile_name(user_id)
            member_ref.set({"user_id": user_id, "display_name": real_name, "updated_at": datetime.utcnow()})
            return real_name
    except Exception:
        pass
    return f"成員({user_id[:4]})"

# ==========================================
# 🌐 4. Webhook 核心主線
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: line_handler.handle(body_str, signature)
    except InvalidSignatureError: pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    if not db: return

    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"

    # 📥 讀取或初始化群組狀態
    current_mode = "normal"
    active_code = ""
    
    if is_group:
        group_doc_ref = db.collection("groups").document(target_id)
        group_snap = group_doc_ref.get()
        if group_snap.exists:
            g_data = group_snap.to_dict()
            current_mode = g_data.get("state", "normal")
            active_code = g_data.get("active_order_code", "")
        else:
            group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})

    # 🚨 【全時段被動 Tag 閘門】
    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@記帳米粒", "記帳米粒"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: return 

    # ====================================================
    # 🎯 🛠️ 【Python 邊緣代打層：核銷雙存同步修正】
    # ====================================================
    is_settle_intent = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    
    if is_group and is_settle_intent:
        code_match = re.search(r'#?(\d{4})', user_text)
        if not code_match:
            send_line_reply(target_id, "⚠️ 請輸入正確的核銷格式！必須包含團購單號才能進行平帳。\n👉 範例：『@記帳米粒 幫＠小明核銷 #1234 金額 500』")
            return
            
        req_code = code_match.group(1)
        
        order_found = None
        orders_query = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", req_code).stream()
        for doc_obj in orders_query:
            order_found = doc_obj.to_dict()
            break
            
        if not order_found:
            send_line_reply(target_id, f"❌ 核銷失敗！找不到本群組內編號為 #{req_code} 的團購單。")
            return
            
        # 激活結算模式
        current_mode = "settle"
        db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": req_code})
        
        # 智慧提取被 Tag 成員
        tagged_user_ids = []
        if mention and mention.mentionees:
            for m in mention.mentionees:
                u_id = getattr(m, "user_id", None)
                if u_id and u_id != creator_id:
                    tagged_user_ids.append(u_id)
        
        # 提取金額
        amount_match = re.search(r'(?:金額|付|給|還)?\s*(\d+)\s*(?:元|塊)?', user_text.replace(req_code, ""))
        settle_amount = int(amount_match.group(1)) if amount_match else 0
        if settle_amount == 0:
            all_nums = re.findall(r'\d+', user_text)
            for n in all_nums:
                if n != req_code: settle_amount = int(n); break

        if settle_amount > 0:
            final_payer_id = None
            final_receiver_id = None
            
            if len(tagged_user_ids) >= 2:
                final_payer_id = tagged_user_ids[0]
                final_receiver_id = tagged_user_ids[1]
            elif len(tagged_user_ids) == 1:
                final_payer_id = tagged_user_ids[0]
                final_receiver_id = creator_id
                
            if final_payer_id and final_receiver_id and final_payer_id != final_receiver_id:
                # 🚀 【雙存修正核心】反查人類真實暱稱
                payer_name_str = resolve_id_to_name(target_id, final_payer_id)
                receiver_name_str = resolve_id_to_name(target_id, final_receiver_id)

                # 寫入資料庫：同時保存 ID 作為 Key，並保存人類暱稱供前端直接勾稽渲染
                db.collection("groups").document(target_id).collection("settlements").document().set({
                    "payer_id": final_payer_id,
                    "receiver_id": final_receiver_id,
                    "payer_name": payer_name_str,       
                    "receiver_name": receiver_name_str,   
                    "amount": settle_amount,
                    "order_code_ref": req_code,
                    "timestamp": datetime.utcnow()
                })
                
                send_line_reply(target_id, f"✅ 【單號 #{req_code} 核銷成功】\n💸 付款人：{payer_name_str}\n📥 收款人：{receiver_name_str}\n💰 金額：${settle_amount:,} 元 已紀錄！")
                return
            else:
                send_line_reply(target_id, f"⚠️ 已為您切換至單號 #{req_code} 結算模式，但核銷成員無效，請重新 Tag 團員。")
                return

    # 常態模式下導流入口
    is_report_intent = any(k in user_text for k in ["報表", "查帳", "大後台", "網址", "入口", "登入"])
    if is_group and current_mode == "normal" and is_report_intent:
        dashboard_url = f"https://liff.line.me/{MY_LIFF_ID}?groupId={target_id}"
        send_line_reply(target_id, f"📊 【記帳米粒 ｜ 雲端監控後台】\n🟢 入口如下：\n{dashboard_url}")
        return

    user_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            send_line_reply(target_id, "🤖 米粒僅為小小的記帳員，請勿探討敏感議題喔！")
            return

    # ====================================================
    # 🧠 第二層：Gemini 核心大腦（處理點單與記帳雙存）
    # ====================================================
    try:
        prompt = f"""
        你是一個高效的財務助理「記帳米粒」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
        請分析使用者訊息：『{user_text}』
        
        【分流任務】：
        1. 判定 intent (record, order_start, order_end, order_item, chat)。
        2. 純輸入金額項目（如：晚餐 350），為 "record"。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
        ).parsed

        # 1. 常態模式普通記帳 (record)
        if result.intent == "record":
            if result.records:
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, 
                            "amount": rec.amount, 
                            "item": rec.item, 
                            "category": rec.category,
                            "timestamp": datetime.utcnow(), 
                            "created_by_uid": creator_id,
                            "created_by_name": creator_name_str  # 🎯 雙存：直接寫入真實人類暱稱
                        })
                send_line_reply(target_id, f"👌 收到！已成功幫 {creator_name_str} 登記一筆花費至雲端後台。")

        # 2. 開團模式 (order_start)
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            db.collection("groups").document(target_id).update({"state": "order", "active_order_code": code_str, "order_items_temp": []})
            send_line_reply(target_id, f"🚀 【團購模式已啟動】\n🔢 本團結算編號：#{code_str}\n👉 請大家叫單時記得「@記帳米粒 品項 金額」喔！")

        # 3. 點餐品項蒐集 (order_item)
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                for item in result.order_items:
                    temp_items.append({
                        "buyer_id": creator_id,
                        "buyer": creator_name_str,  # 🎯 雙存：前端對帳單將精準勾稽這個真實姓名字串
                        "item": item.item_name, 
                        "price": item.price, 
                        "timestamp": datetime.utcnow().isoformat()
                    })
                g_ref.update({"order_items_temp": temp_items})
                send_line_reply(target_id, f"📝 收到！已幫 {creator_name_str} 掛載點單品項。")

        # 4. 截止結單 (order_end)
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                total_amt = sum(i["price"] for i in temp_items)
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                
                g_ref.collection("orders").document(f"{datetime.now().strftime('%Y%m%d')}_{code_str}").set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), 
                    "order_code": code_str, 
                    "total_amount": total_amt,
                    "master_payer_id": creator_id,
                    "master_payer_name": creator_name_str,  # 🎯 雙存：買單墊款人同步存入暱稱
                    "items": temp_items, 
                    "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"🏁 【團購截止 ｜ 單號 #{code_str}】\n💰 總金額：${total_amt} 元\n💳 墊款買單：{creator_name_str}\n\n🤖 數據已更新！已恢復正常模式。")
            else:
                send_line_reply(target_id, "🛑 因無人叫單，本團已直接關閉。")
                
            g_ref.update({"state": "normal", "order_items_temp": []})

        # 5. 手動關閉結算模式
        elif "結算結束" in user_text and current_mode == "settle" and is_group:
            db.collection("groups").document(target_id).update({"state": "normal"})
            send_line_reply(target_id, "🔓 結算完畢！已恢復正常模式。")

        # 6. 簡單閒聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(target_id, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "synchronized_dual_saving_active", "version": "v7.0-Final-SaaS"}
