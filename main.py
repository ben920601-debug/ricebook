import os
import re
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3 (重新引回推播功能，供 Python 第一層與 Gemini 互動回覆)
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

app = FastAPI(title="飯糰小幫手 ｜ 雙層責任防禦省成本版")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET) # 🎯 修正：變數名稱100%對齊 line_handler
ai_client = genai.Client(api_key=GEMINI_API_KEY)

if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] 成功建立 Firestore 雙軌安全連線通道！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域狀態機與強型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項還錢的人名字。若自稱我請填寫『發話者』")
    receiver_name: str = Field(description="收到款項拿回錢的人名字。若自稱我請填寫『發話者』")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐者名字。若自稱我或空白請寫『發話者』")
    item_name: str = Field(description="品項名稱")
    price: int = Field(description="單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流。record:普通記帳, chat:普通簡單對話互動"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    target_payer: Optional[str] = Field(default="")
    ai_reply: Optional[str] = Field(default="", description="Gemini 給予使用者的簡短對話互動回覆文字")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def get_cached_nickname(target_id: str, user_id: str, is_group: bool) -> str:
    if not db: return "記帳夥伴"
    if not is_group: return "個人帳本主"
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists: return doc_snap.to_dict().get("display_name", "群組夥伴")
    except Exception: pass
    return "群組夥伴"

def send_line_reply(target_id: str, text: str):
    """🤖 統一調度 LINE SDK 推播回覆"""
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
    except Exception as e:
        print(f"❌ LINE 推播挫敗: {e}", flush=True)

# ==========================================
# 🌐 4. Webhook 入口與雙層責任防禦線
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

    # ====================================================
    # 🛑 🛡️ 🟥 第一層防禦：Python 邊緣硬核規則與敏感詞篩選
    # ====================================================
    
    # A. 敏感字絕對防禦
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            send_line_reply(target_id, "🤖 飯糰小幫手為純財務平帳系統，請勿探討敏感、情色、違法或政治議題喔！")
            return

    # B. Python 第一回覆：純固定對話/記帳格式規定觸發 (免扣 Gemini Token)
    if user_text in ["使用說明", "怎麼用", "功能", "記帳格式", "規定"]:
        instructions = (
            "📝 【飯糰小幫手 ｜ 使用說明書】\n"
            "-------------------------\n"
            "💡 「個人記帳」：直接輸入即可！\n"
            "👉 範例：『午餐 120』、『高鐵 1300』\n\n"
            "👥 「群組公帳」：(需在文字內加上 飯糰 二字)\n"
            "👉 範例：『飯糰 公開花費 500』\n\n"
            "🛒 「揪團開單狀態機」：\n"
            "👉 輸入 『飯糰 開團』 ➡️ 進入過濾點單狀態\n"
            "👉 輸入 『品項 金額』 ➡️ 網頁自動掛載\n"
            "👉 輸入 『飯糰 結單』 ➡️ 封存並生成控制代碼\n"
            "👉 輸入 『訂單結算 #單號』 ➡️ 查看紅綠燈對帳報表"
        )
        send_line_reply(target_id, instructions)
        return

    # C. Python 第一回覆：個人私帳引流
    if not is_group and user_text in ["查帳", "後台", "網址", "登入", "進去"]:
        db.collection("users").document(target_id).update({"last_active_time": datetime.utcnow(), "verified_uid": target_id})
        send_line_reply(target_id, f"🌐 您的個人無痕雲端帳本後台已就緒：\nhttps://liff.line.me/{MY_LIFF_ID}?userId={target_id}")
        return

    # D. 讀取或初始化 Firestore 當前模式狀態
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

    # E. 群組聊天防噪牆：判定這句話「值不值得發送給 Gemini」
    is_triggered = False
    if not is_group:
        is_triggered = True  # 個人私聊對話放行
    else:
        if any(kw in user_text for kw in ["@飯糰", "飯糰", "開團", "結單", "結算", "對帳", "誰沒", "未付"]): 
            is_triggered = True
        elif current_mode == "order" and re.search(r'\d+', user_text): 
            is_triggered = True
        elif current_mode == "settle" and any(k in user_text for k in ["給", "還", "付"]): 
            is_triggered = True

    if not is_triggered: 
        return  # 🎯 毫無特徵的日常閒聊，Python 直接就地阻斷，Gemini 負擔降為 0！

    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()
    creator_name = get_cached_nickname(target_id, creator_id, is_group)

    # ====================================================
    # 🧠 🧠 🟨 第二層防禦：Gemini 核心大腦（高智慧低消耗）
    # ====================================================
    try:
        prompt = f"""
        你是一個幽默、控場能力極強的記帳助理「飯糰小幫手」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
        請透視分析使用者訊息：『{user_text}』
        
        【任務說明】：
        1. 判定 intent (record, order_start, order_end, order_item, settle_start, settle_pay, settle_query, chat)。
        2. 如果對話有包含金額（例如：點了雞排 90、我給了阿誠 150），除了拆解強型別欄位外，請在 ai_reply 留下一句 15 字以內非常簡短的俏皮確認對話。
        3. 如果只是純問候或打招呼（例如：嗨、哈囉、謝謝），將 intent 設為 "chat"，並在 ai_reply 簡單幽默回覆。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.2),
        ).parsed

        # --- 數據默默直寫 Firestore（0 訊息費） ---
        
        # 記帳分流 (record)
        if result.intent == "record" and current_mode == "normal":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category,
                            "timestamp": datetime.utcnow(), "created_by_name": creator_name
                        })
                # 🪙 只有「收到金額含對話」或觸發時，才用 LINE 回覆簡短對話，極省額度！
                if result.ai_reply:
                    send_line_reply(target_id, f"🤖 {result.ai_reply} (已實時記入雲端後台)")

        # 開團 (order_start)
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            db.collection("groups").document(target_id).update({"state": "order", "active_order_code": code_str, "order_items_temp": []})
            send_line_reply(target_id, f"🚀 【飯團團購模式・正式啟動】\n🔢 本團結算編號：#{code_str}\n👉 大家可以開始叫單囉！(網頁後台將實時動態掛載)")

        # 點單搜集 (order_item)
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                for item in result.order_items:
                    buyer = creator_name if not item.buyer_name or item.buyer_name == "發話者" else item.buyer_name.strip()
                    temp_items.append({"buyer": buyer, "item": item.item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()})
                g_ref.update({"order_items_temp": temp_items})
                
                # 點單不發大篇幅文字，只回覆 Gemini 的一句超精簡確認對話（省額度）
                if result.ai_reply:
                    send_line_reply(target_id, f"📝 {result.ai_reply}")

        # 截止結單 (order_end)
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                m_payer = creator_name if not result.target_payer or result.target_payer == "發話者" else result.target_payer.strip()
                total_amt = sum(i["price"] for i in temp_items)
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                
                g_ref.collection("orders").document(f"{datetime.now().strftime('%Y%m%d')}_{code_str}").set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), "order_code": code_str, "total_amount": total_amt,
                    "master_payer_name": m_payer, "items": temp_items, "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"🏁 【團購安全截止 ｜ 單號 #{code_str}】\n💰 總開銷：${total_amt} 元\n💳 墊款買單：{m_payer}\n🤖 資料已封存，請輸入「訂單結算 #{code_str}」發動控制台。")
            g_ref.update({"state": "normal", "order_items_temp": []})

        # 啟動結算催款控制台 (settle_start)
        elif result.intent == "settle_start" and is_group:
            match_code = re.search(r'(\d{4})', user_text)
            if match_code:
                req_code = match_code.group(1)
                db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": req_code})
                send_line_reply(target_id, f"🔔 【催款控制台已降臨 ｜ 結算模式】\n🔢 活躍單號：#{req_code}\n🌐 網頁端已同步解鎖「紅綠燈對帳報表」！核銷完畢請輸入『結算結束』。")

        # 登記付款核銷 (settle_pay)
        elif result.intent == "settle_pay" and current_mode == "settle" and is_group:
            if result.settlement:
                s = result.settlement
                p_name = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name.strip()
                r_name = master_payer_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name.strip()
                
                if p_name != r_name:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p_name, "receiver_name": r_name, "amount": s.amount, "order_code_ref": active_code, "timestamp": datetime.utcnow()
                    })
                if result.ai_reply:
                    send_line_reply(target_id, f"🤝 {result.ai_reply}")

        # 智慧查詢對帳明細 (settle_query) -> ⭐️ 只有主動查帳才輸出大報表，大幅縮減日常推播
        elif result.intent == "settle_query" and current_mode == "settle" and is_group:
            # 這裡保留原有的聯集計算比對邏輯，生成已付與未付明細報表
            orders = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", active_code).stream()
            order_doc = null
            for doc in orders: order_doc = doc.to_dict(); break
            
            if order_doc:
                expected = {}; [expected.update({i["buyer"]: expected.get(i["buyer"], 0) + i["price"]}) for i in order_doc.get("items", [])]
                settles = db.collection("groups").document(target_id).collection("settlements").where("order_code_ref", "==", active_code).stream()
                actual = {}; [actual.update({s.to_dict().get("payer_name"): actual.get(s.to_dict().get("payer_name"), 0) + s.to_dict().get("amount", 0)}) for s in settles]
                
                unpaid = [f" 🔴 {p} 尚未付款 (應付：${amt})" if actual.get(p,0)==0 else f" 🟡 {p} 已局部付 ${actual[p]} (還差 ${amt-actual[p]})" for p, amt in expected.items() if actual.get(p, 0) < amt]
                reply_report = f"📊 【訂單 #{active_code} 實時催款明細】\n-------------------------\n" + ("🎉 全員皆已付清！" if not unpaid else "\n".join(unpaid))
                send_line_reply(target_id, reply_report)

        # 簡單對話互動 (chat)
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(target_id, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 第二層 Gemini 大腦運算或欄位解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "optimized_dual_layer_active", "version": "v5.5-SaaS-CostSaving"}
