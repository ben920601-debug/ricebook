import os
import re
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3 (僅保留 Webhook 簽章驗證與事件解析，完全不呼叫 Push 主動推播 API)
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

app = FastAPI(title="飯糰小幫手 ｜ SaaS 雙軌無消耗核心大腦")

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
        print("🔥 [DATABASE] 成功建立 Firestore 雙軌安全通道！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域強型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

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
    buyer_name: str = Field(description="點餐或購買這個東西的人的名字，如果自稱我，請寫『發話者』")
    item_name: str = Field(description="購買的品項名稱")
    price: int = Field(description="該品項的單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "sensitive", "settlement", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流。order_item:點單明細, order_end:結單, order_start:開團, settle_start:進入結算, settle_pay:登記收付款, settle_query:查詢對帳明細"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list, description="點單模式中拆解出來的品項與金額清單")
    target_payer: Optional[str] = Field(default="", description="訂單結束時，指定最後買單付款的人名字")
    target_order_id: Optional[str] = Field(default="", description="結算模式中，使用者輸入的日期與編號代碼，例如 0620 #8821")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 歷史暱稱快取池工具組
# ==========================================
def get_cached_nickname(target_id: str, user_id: str, is_group: bool) -> str:
    """👥 暱稱快取池：優先從本地資料庫撈取，阻斷頻繁請求 LINE 官方 API 造成的時差與費用"""
    if not db: 
        return "記帳夥伴"
    if not is_group: 
        return "個人帳本主"
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", "群組夥伴")
    except Exception: 
        pass
    return "群組夥伴"

def analyze_with_gemini_sync(user_text: str, root_collection: str, current_mode: str = "normal") -> SuperRouter:
    """🧠 核心大腦：根據目前的架構環境 (users/groups) 與狀態機模式，精準拆解語意"""
    prompt = f"""
    你是一個具備頂級控場能力的財務記帳助理「飯糰小幫手」。目前位於【{root_collection}】環境，群組模式處於【{current_mode}】。
    請分析使用者的語意輸入：『{user_text}』進行強型別分流。
    
    【核心分流規則】：
    1. 如果提及「開啟訂單」、「團購開始」、「開團」，intent 務必歸為 order_start。
    2. 如果提及「訂單結束」、「結單」、「截止」，intent 務必歸為 order_end。
    3. 如果提及「訂單結算」、「結算訂單」，intent 務必歸為 settle_start。
    4. 如果是普通的生活花費（如：午餐 120、高鐵 1200），intent 務必歸為 record。
    """
    
    if current_mode == "order":
        prompt += """
        5. 當前為【訂單模式】：群組成員正在熱烈點單。只要有提到任何品項和金額（例如：牛肉麵 150、小明 雞排 95），
           請將 intent 歸類為 order_item，並精準拆解到 order_items 陣列中（若自稱我，買家名字請填寫『發話者』）。
           如果只是日常聊天雜訊，請直接將 intent 歸類為 chat，且 ai_reply 留空。
        """
    elif current_mode == "settle":
        prompt += """
        6. 當前為【結算模式】：成員正在主動交錢還款。
           - 如果使用者是在問「誰沒給錢」、「誰未給錢」、「未付明細」、「對帳」，intent 務必歸為 settle_query。
           - 如果使用者輸入符合「我給了 @阿誠 150」、「小明 還 墊款人 95」，intent 務必歸為 settle_pay，並拆解到 settlement 結構中。
           - 如果是其他任何無關的閒聊雜訊，請將 intent 歸類為 chat，且 ai_reply 留空。
        """

    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
    )
    if response.parsed: 
        return response.parsed
    return SuperRouter(**json.loads(response.text))

# ==========================================
# 🌐 4. Webhook 入口與狀態機調度主線 (純寫入，免消耗額度)
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
        handler.handle(body_str, signature)
    except InvalidSignatureError: 
        pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    if not db: 
        return

    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    
    # 🎯 雙軌導航路由：群組寫進 groups 集合，個人私聊寫進 users 集合
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"

    # 📥 A. 從 Firestore 實時調閱群組當前模式狀態（確保與網頁前端 100% 同步連動）
    current_mode = "normal"
    active_code = ""
    master_payer_name = ""
    
    if is_group:
        group_doc_ref = db.collection("groups").document(target_id)
        group_snap = group_doc_ref.get()
        if group_snap.exists:
            g_data = group_snap.to_dict()
            current_mode = g_data.get("state", "normal")
            active_code = g_data.get("active_order_code", "")
            master_payer_name = g_data.get("master_payer", "")
        else:
            group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})
    else:
        user_doc_ref = db.collection("users").document(target_id)
        if not user_doc_ref.get().exists:
            user_doc_ref.set({"user_id": target_id, "created_at": datetime.utcnow()})

    # 🚀 B. 【核心群組防噪與節流過濾機制】
    is_triggered = False
    if not is_group:
        is_triggered = True  # 個人一對一對話全部放行
    else:
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開團", "結單", "結算", "對帳"]): 
            is_triggered = True
        elif current_mode == "order" and re.search(r'\d+', user_text): 
            is_triggered = True
        elif current_mode == "settle" and any(k in user_text for k in ["給", "還", "付", "誰沒", "未付"]): 
            is_triggered = True
        
        if not is_triggered:
            return # 判定為閒聊雜訊，直接中斷攔截，省下 Gemini Token 與運算成本

        user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()

    # 🛑 攔截機制：敏感詞防禦
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            return  # 靜音中斷

    creator_name = get_cached_nickname(target_id, creator_id, is_group)

    # 🧠 C. 進入大腦運算核心
    try:
        result = analyze_with_gemini_sync(user_text, root_collection, current_mode)

        # ----------------------------------------------------
        # 模式 1：常態模式下的普通記帳 (record) ➡️ 支援雙軌直寫
        # ----------------------------------------------------
        if result.intent == "record" and current_mode == "normal":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, 
                            "amount": rec.amount, 
                            "item": rec.item, 
                            "category": rec.category,
                            "note": rec.note,
                            "timestamp": datetime.utcnow(), 
                            "created_by_uid": creator_id,
                            "created_by_name": creator_name
                        })

        # ----------------------------------------------------
        # 模式 2：開啟訂單模式 (order_start) ➡️ 僅限群組公帳
        # ----------------------------------------------------
        elif result.intent == "order_start" and is_group:
            db.collection("groups").document(target_id).update({
                "state": "order",
                "active_order_code": str(random.randint(1000, 9999)),
                "order_items_temp": []  # 建立開團暫存池
            })

        # ----------------------------------------------------
        # 模式 3：點單品項自動蒐集暫存 (order_item) ➡️ 僅限群組公帳
        # ----------------------------------------------------
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                
                for item in result.order_items:
                    buyer = item.buyer_name.strip()
                    if buyer == "發話者" or not buyer: 
                        buyer = creator_name
                    temp_items.append({
                        "buyer": buyer, 
                        "item": item.item_name, 
                        "price": item.price,
                        "timestamp": datetime.utcnow().isoformat()
                    })
                g_ref.update({"order_items_temp": temp_items})

        # ----------------------------------------------------
        # 模式 4：訂單結束、生成單號正式封存 (order_end) ➡️ 僅限群組公帳
        # ----------------------------------------------------
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                master_payer = result.target_payer.strip() if result.target_payer else creator_name
                if master_payer == "發話者": 
                    master_payer = creator_name
                
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                order_doc_id = f"{datetime.now().strftime('%Y%m%d')}_{code_str}"
                total_amt = sum(i["price"] for i in temp_items)
                
                # 寫入正式 orders 子集合
                g_ref.collection("orders").document(order_doc_id).set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"),
                    "order_code": code_str,
                    "total_amount": total_amt,
                    "master_payer_name": master_payer,
                    "items": temp_items,
                    "status": "pending",
                    "timestamp": datetime.utcnow()
                })
            
            # 大後台同步解除鎖定，回歸常態模式
            g_ref.update({"state": "normal", "order_items_temp": []})

        # ----------------------------------------------------
        # 模式 5：啟動訂單催款結算控制台 (settle_start) ➡️ 僅限群組公帳
        # ----------------------------------------------------
        elif result.intent == "settle_start" and is_group:
            match_code = re.search(r'(\d{4})', user_text)
            if match_code:
                req_code = match_code.group(1)
                # 實時推更後台狀態機為結算模式
                db.collection("groups").document(target_id).update({
                    "state": "settle",
                    "active_order_code": req_code
                })

        # ----------------------------------------------------
        # 模式 6：結算模式下的勾稽付款登記 (settle_pay) ➡️ 僅限群組公帳
        # ----------------------------------------------------
        elif result.intent == "settle_pay" and current_mode == "settle" and is_group:
            if result.settlement:
                s = result.settlement
                p_name = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name.strip()
                r_name = master_payer_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name.strip()
                
                if p_name != r_name:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p_name, 
                        "receiver_name": r_name, 
                        "amount": s.amount,
                        "order_code_ref": active_code, 
                        "timestamp": datetime.utcnow()
                    })

        # ----------------------------------------------------
        # 模式 7：手動關閉或指令結束結算
        # ----------------------------------------------------
        elif "結算結束" in user_text and current_mode == "settle" and is_group:
            db.collection("groups").document(target_id).update({"state": "normal"})

        # 🚀 新增：個人私帳路徑專屬身分引路引流器
        elif not is_group and any(k in user_text for k in ["查帳", "後台", "網址", "登入"]):
            db.collection("users").document(target_id).update({
                "last_active_time": datetime.utcnow(),
                "verified_uid": target_id  # 寫入正確的真實 UID 供備查
            })

    except Exception as e:
        print(f"🧠 雙軌無消耗大腦處理流出錯: {e}")

@app.get("/")
def health_check(): 
    return {"status": "silent_dual_engine_active", "version": "v5.0-SaaS-SilentDualEngine"}
