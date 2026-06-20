import os
import re
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3 (僅用於 Webhook 簽章驗證與解析，不使用主動 push 訊息功能)
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="飯糰小幫手 ｜ 後端大腦無消耗流暢版")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Firebase Firestore 初始化驗證
if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] Firestore 安全連線通道就位！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 強型別語意路由定義
# ==========================================
class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項、要把錢還給別人的那個人名字。如果使用者說的是『我』，請填寫『發話者』")
    receiver_name: str = Field(description="收到款項、拿回錢的那個人名字。如果使用者說的是『我』，請填寫『發話者』")
    amount: int = Field(default=0, description="還錢的具體金額")

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐者的名字，如果自稱我或空白，請寫『發話者』")
    item_name: str = Field(description="品項名稱")
    price: int = Field(description="單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流。order_item:點單明細, order_end:結單, order_start:開團, settle_start:催款結算, settle_pay:登記收付款, settle_query:查詢對帳明細"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    target_payer: Optional[str] = Field(default="", description="指定的墊款買單人名字")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def get_cached_nickname(target_id: str, user_id: str) -> str:
    """👥 優化方向：資料庫本地暱稱快取池，避免頻繁請求 LINE API 造成時差與額度浪費"""
    if not db:
        return "群組夥伴"
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", "群組夥伴")
    except Exception:
        pass
    return "群組夥伴"

def analyze_with_gemini_sync(user_text: str, current_mode: str = "normal") -> SuperRouter:
    """🧠 核心大腦：精準拆解群組上下文模式意圖"""
    prompt = f"""
    你是一個記帳助理「飯糰小幫手」。目前群組處於【{current_mode}】模式。
    請分析使用者的語意輸入：『{user_text}』進行強型別分流。
    
    【分流規則】：
    1. 提及「開啟訂單」、「團購開始」、「開團」，intent 務必為 order_start。
    2. 提及「訂單結束」、「結單」、「截止」，intent 務必為 order_end。
    3. 提及「訂單結算」、「結算訂單」，intent 務必為 settle_start。
    """
    if current_mode == "order":
        prompt += """
        4. 當前為【訂單模式】：點單進行中。提及品項和金額（如：牛肉麵 150、小明 雞排 95），
           intent 為 order_item，拆解到 order_items 陣列。若沒寫名字，買家名字填寫『發話者』。
           若只是純日常聊天（如：這家好吃），intent 歸為 chat，且 ai_reply 留空。
        """
    elif current_mode == "settle":
        prompt += """
        5. 當前為【結算模式】：核銷收還款中。
           - 詢問「誰沒給錢」、「對帳明細」、「誰未付款」，intent 務必為 settle_query。
           - 輸入符合「我給了 @阿誠 150」、「小明 還 墊款人 95」，intent 務必為 settle_pay（若自稱我，名字填『發話者』）。
           - 其他閒聊雜訊，intent 歸為 chat，ai_reply 留空。
        """
    else:
        prompt += """
        6. 當前為【常態模式】：支援普通公帳 record 與普通平帳核銷 settlement。
        """

    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
    )
    return response.parsed if response.parsed else SuperRouter(**json.loads(response.text))

# ==========================================
# 🌐 4. Webhook 核心流動控制（純寫入 Firestore，免消耗推播）
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: 
        raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: 
        line_handler.handle(body_str, signature)
    except InvalidSignatureError: 
        pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    if not db: 
        return
    
    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    target_id = event.source.group_id if is_group else creator_id

    # 📥 1. 實時從 Firestore 撈取群組目前的真實模式狀態（與前端連動）
    group_doc_ref = db.collection("groups").document(target_id)
    group_snap = group_doc_ref.get()
    
    current_mode = "normal"
    active_code = ""
    master_payer_name = ""
    
    if group_snap.exists:
        g_data = group_snap.to_dict()
        current_mode = g_data.get("state", "normal")
        active_code = g_data.get("active_order_code", "")
        master_payer_name = g_data.get("master_payer", "")
    else:
        # 初始化群組基礎文件
        group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})

    # 👥 2. 去噪過濾：群組對話防噪機制
    is_triggered = False
    if not is_group:
        is_triggered = True
    else:
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開團", "結單", "結算"]):
            is_triggered = True
        elif current_mode == "order" and re.search(r'\d+', user_text):
            is_triggered = True
        elif current_mode == "settle" and any(k in user_text for k in ["給", "還", "付", "誰沒", "未付"]):
            is_triggered = True

    if not is_triggered:
        return  # 🎯 雜訊直接阻斷，零成本

    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()
    creator_name = get_cached_nickname(target_id, creator_id)

    # 🧠 3. 送入大腦拆解
    try:
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"模式:{current_mode}, 內容:{user_text}",
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1)
        ).parsed
        
        # ----------------------------------------------------
        # 核心意圖處理 ➡️ 全面改為【直寫 Firestore】免發 LINE 訊息
        # ----------------------------------------------------
        
        # A. 開團 (order_start)
        if result.intent == "order_start":
            group_doc_ref.update({
                "state": "order",
                "active_order_code": str(random.randint(1000, 9999)),
                "order_items_temp": []
            })

        # B. 點餐品項蒐集 (order_item)
        elif result.intent == "order_item" and current_mode == "order":
            if result.order_items:
                # 先抓出暫存的 items
                g_data = group_doc_ref.get().to_dict()
                temp_items = g_data.get("order_items_temp", [])
                
                for item in result.order_items:
                    buyer = item.buyer_name.strip()
                    if buyer == "發話者" or not buyer: 
                        buyer = creator_name
                    temp_items.append({
                        "buyer": buyer, "item": item.item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()
                    })
                group_doc_ref.update({"order_items_temp": temp_items})

        # C. 截止結單 (order_end)
        elif result.intent == "order_end" and current_mode == "order":
            g_data = group_doc_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                m_payer = result.target_payer.strip() if result.target_payer else creator_name
                if m_payer == "發話者": m_payer = creator_name
                
                total_amt = sum(i["price"] for i in temp_items)
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                order_doc_id = f"{datetime.now().strftime('%Y%m%d')}_{code_str}"
                
                # 封存正式訂單
                group_doc_ref.collection("orders").document(order_doc_id).set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"),
                    "order_code": code_str,
                    "total_amount": total_amt,
                    "master_payer_name": m_payer,
                    "items": temp_items,
                    "timestamp": datetime.utcnow()
                })
            
            # 回歸常態模式
            group_doc_ref.update({"state": "normal", "order_items_temp": []})

        # D. 開啟催款控制台 (settle_start)
        elif result.intent == "settle_start":
            match_code = re.search(r'(\d{4})', user_text)
            if match_code:
                req_code = match_code.group(1)
                group_doc_ref.update({
                    "state": "settle",
                    "active_order_code": req_code
                })

        # E. 登記付款核銷 (settle_pay)
        elif result.intent == "settle_pay" and current_mode == "settle":
            if result.settlement:
                s = result.settlement
                p_name = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name
                r_name = master_payer_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name
                
                if p_name != r_name:
                    group_doc_ref.collection("settlements").document().set({
                        "payer_name": p_name, "receiver_name": r_name, "amount": s.amount,
                        "order_code_ref": active_code, "timestamp": datetime.utcnow()
                    })

        # F. 常態模式記帳 (record)
        elif result.intent == "record" and current_mode == "normal":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        group_doc_ref.collection("expenses").document().set({
                            "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category,
                            "timestamp": datetime.utcnow(), "created_by_name": creator_name
                        })

    except Exception as e:
        print(f"🧠 大腦無消耗處理流出錯: {e}")

@app.get("/")
def health_check(): 
    return {"status": "silent_mode_active"}
