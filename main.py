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
    ReplyMessageRequest,
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

app = FastAPI(title="記帳米粒 ｜ 暱稱完美顯示版")

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
    print("❌ [DATABASE] 嚴重錯誤：未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")

class SingleSettlement(BaseModel):
    payer_name: str = Field(default="")
    receiver_name: str = Field(default="")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(default="")
    item_name: str = Field(default="")
    price: int = Field(default=0)

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "order_start", "order_end", "order_item", "settle_start", "settle_pay", "settle_end"] = Field(
        description="核心意圖分流"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    ai_reply: Optional[str] = Field(default="", description="與使用者的聊天回應")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def send_line_reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        print(f"❌ LINE 回覆失敗: {e}", flush=True)

def get_real_mentions(event) -> list:
    """🎯 核心修復：過濾掉機器人自身的 Tag，只抓取真實成員的 ID"""
    real_tagged_ids = []
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees:
        text = getattr(event.message, "text", "")
        for m in mention.mentionees:
            u_id = getattr(m, "user_id", None)
            if u_id:
                try:
                    tagged_text = text[m.index : m.index + m.length]
                    # 如果 Tag 到的名字包含「米粒」，判定為機器人自己，直接略過
                    if "米粒" in tagged_text:
                        continue
                except:
                    pass
                real_tagged_ids.append(u_id)
    return real_tagged_ids

def fetch_line_profile_name(user_id: str, target_id: str = None) -> str:
    """🎯 核心修復：升級為群組成員 API，未加好友也能抓到真實暱稱"""
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    
    # 1. 優先嘗試「群組成員 API」
    if target_id and target_id.startswith("C"):
        url = f"https://api.line.me/v2/bot/group/{target_id}/member/{user_id}"
        try:
            res = httpx.get(url, headers=headers, timeout=5.0)
            if res.status_code == 200:
                return res.json().get("displayName", f"成員({user_id[:4]})")
        except Exception:
            pass
            
    # 2. 退回使用「全域好友 API」
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            return res.json().get("displayName", f"成員({user_id[:4]})")
    except Exception:
        pass
        
    return f"成員({user_id[:4]})"

def resolve_id_to_name(target_id: str, user_id: str) -> str:
    if not db or not user_id: return "群組夥伴"
    if not user_id.startswith("U"): return user_id
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", f"成員({user_id[:4]})")
        else:
            # 傳遞 target_id 給 fetch_line_profile_name 以觸發群組 API
            real_name = fetch_line_profile_name(user_id, target_id)
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
    reply_token = event.reply_token 
    
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"

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

    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@記帳米粒", "記帳米粒"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: return 

    # ====================================================
    # 🎯 🛠️ 【核銷解鎖與防呆邏輯】
    # ====================================================
    is_settle_trigger = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    if is_group and current_mode == "normal" and is_settle_trigger:
        code_match = re.search(r'#?(\d{4})', user_text)
        if not code_match:
            send_line_reply(reply_token, "⚠️ 必須輸入對應的 4 位數團購單號才可開啟核銷模式。\n👉 範例：『@記帳米粒 申請核銷 #1234』")
            return
            
        req_code = code_match.group(1)
        order_found = None
        orders_query = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", req_code).stream()
        for doc_obj in orders_query: order_found = doc_obj.to_dict(); break
            
        if not order_found:
            send_line_reply(reply_token, f"❌ 找不到本群組內編號為 #{req_code} 的團購單。")
            return
            
        db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": req_code})
        payer_str = resolve_id_to_name(target_id, order_found.get('master_payer_id', creator_id))
        send_line_reply(reply_token, f"🔓 成功解鎖結算模式！鎖定單號：#{req_code}\n💳 墊款買單人：{payer_str}\n👉 請開始核銷對帳（如：@記帳米粒 我核銷我自己 150）")
        return

    # ====================================================
    # 🎯 🛠️ 【結算模式：互相核銷與自行核銷】
    # ====================================================
    if is_group and current_mode == "settle":
        if any(k in user_text for k in ["結算結束", "關閉結算", "退出結算", "核銷完畢"]):
            db.collection("groups").document(target_id).update({"state": "normal", "active_order_code": ""})
            send_line_reply(reply_token, "🔓 結算完畢！已安全關閉對帳並恢復常態模式。")
            return

        if any(k in user_text for k in ["給", "還", "付", "收", "核銷"]):
            clean_text = re.sub(r'#?\d{4}', '', user_text)
            amount_match = re.search(r'\d+', clean_text)
            settle_amount = int(amount_match.group()) if amount_match else 0
            if settle_amount <= 0: return

            # 🎯 使用新的智慧 Tag 過濾機制
            real_tagged_ids = get_real_mentions(event)

            if len(real_tagged_ids) >= 2:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = real_tagged_ids[1]
            elif len(real_tagged_ids) == 1:
                final_payer_id = real_tagged_ids[0]
                final_receiver_id = creator_id
            else:
                final_payer_id = creator_id
                final_receiver_id = creator_id
                
            if final_payer_id and final_receiver_id:
                order_query = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", active_code).stream()
                current_order = None
                for doc_obj in order_query: current_order = doc_obj.to_dict(); break
                if not current_order: return
                
                payer_expected_total = sum(item.get("price", 0) for item in current_order.get("items", []) if item.get("buyer_id") == final_payer_id or item.get("buyer") == final_payer_id)
                history_settles = db.collection("groups").document(target_id).collection("settlements").where("order_code_ref", "==", active_code).where("payer_id", "==", final_payer_id).stream()
                payer_already_paid = sum(doc_obj.to_dict().get("amount", 0) for doc_obj in history_settles)
                remaining_debt = payer_expected_total - payer_already_paid
                
                if remaining_debt <= 0:
                    send_line_reply(reply_token, f"❌ 登記拒絕！成員 {resolve_id_to_name(target_id, final_payer_id)} 在單號 #{active_code} 中並無欠款紀錄。")
                    return
                elif settle_amount > remaining_debt:
                    send_line_reply(reply_token, f"❌ 入帳失敗！金額溢繳！\n⚠️ 該成員此單賸餘應付為：${remaining_debt} 元，您輸入的 ${settle_amount} 元不符合規範，拒絕入帳。")
                    return
                
                payer_name_str = resolve_id_to_name(target_id, final_payer_id)
                receiver_name_str = resolve_id_to_name(target_id, final_receiver_id)

                db.collection("groups").document(target_id).collection("settlements").document().set({
                    "payer_id": final_payer_id, "receiver_id": final_receiver_id,
                    "payer_name": payer_name_str, "receiver_name": receiver_name_str,   
                    "amount": settle_amount, "order_code_ref": active_code, "timestamp": datetime.utcnow()
                })

                if final_payer_id == final_receiver_id:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n🙋‍♂️ 自行核銷：{payer_name_str}\n💰 紀錄金額：${settle_amount}")
                else:
                    send_line_reply(reply_token, f"✅ 【單號 #{active_code} 核銷成功】\n💸 付款：{payer_name_str}\n📥 收款：{receiver_name_str}\n💰 紀錄金額：${settle_amount}")
                return

    clean_text = user_text.replace("@記帳米粒", "").replace("記帳米粒", "").strip()

    # ====================================================
    # 📖 【Python 層攔截：系統說明書與報表派發】
    # ====================================================
    if any(k in clean_text for k in ["報表", "查帳", "大後台", "網址", "入口", "登入"]) and current_mode == "normal":
        send_line_reply(reply_token, f"📊 【記帳米粒 ｜ 雲端監控後台】\n🟢 入口如下：\nhttps://liff.line.me/{MY_LIFF_ID}?groupId={target_id}")
        return

    if any(k in clean_text for k in ["使用說明", "怎麼用", "功能", "規定", "教學"]):
        instructions = (
            "📝 【記帳米粒 ｜ 使用說明書】\n"
            "-------------------------\n"
            "💡 「常態模式記帳」：\n"
            "👉 範例：『@記帳米粒 午餐 120』\n\n"
            "🛒 「團購模式：代點單」：\n"
            "👉 啟動：『@記帳米粒 開團』\n"
            "👉 自己點：『@記帳米粒 雞排 100』\n"
            "👉 幫人點：『@記帳米粒 @小明 珍奶 50』\n"
            "👉 結單：『@記帳米粒 結單』\n\n"
            "💳 「核銷模式：防呆平帳」：\n"
            "👉 啟動：『@記帳米粒 申請核銷 #單號』\n"
            "👉 代收：『@記帳米粒 @小明 給我 100』\n"
            "👉 自核：『@記帳米粒 我核銷 100』\n"
            "👉 關閉：『@記帳米粒 結算結束』"
        )
        send_line_reply(reply_token, instructions)
        return

    for kw in SENSITIVE_KEYWORDS:
        if kw in clean_text:
            send_line_reply(reply_token, "🤖 米粒為純財務助理，請勿探討敏感議題喔！")
            return

    # ====================================================
    # ⚡ 🚀 【Python 第一層極速攔截：代點單與記帳直通落庫】
    # ====================================================
    fast_match = re.fullmatch(r'^(.+?)\s*(\d+)\s*(?:元|塊)?$', clean_text)
    if fast_match and current_mode in ["normal", "order"]:
        raw_item_name = fast_match.group(1).strip()
        amount = int(fast_match.group(2))
        
        item_name = re.sub(r'@\S+', '', raw_item_name).strip()
        
        if not item_name.isdigit() and amount > 0:
            if current_mode == "normal":
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                db.collection(root_collection).document(target_id).collection("expenses").document().set({
                    "type": "expense", "amount": amount, "item": item_name, "category": "生活雜費",
                    "timestamp": datetime.utcnow(), "created_by_uid": creator_id, "created_by_name": creator_name_str
                })
                send_line_reply(reply_token, f"✅ 已紀錄：{item_name} ${amount}")
                return
                
            elif current_mode == "order" and is_group:
                # 🎯 使用新的智慧 Tag 過濾機制
                real_tagged_ids = get_real_mentions(event)
                        
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                temp_items.append({
                    "buyer_id": actual_buyer_id, "buyer": actual_buyer_name,
                    "item": item_name, "price": amount, "timestamp": datetime.utcnow().isoformat()
                })
                g_ref.update({"order_items_temp": temp_items})
                send_line_reply(reply_token, f"📝 已接單：{item_name} ${amount}")
                return

    # ====================================================
    # 🧠 🧠 【第二層：Gemini 核心大腦 - 複雜萃取與自然陪聊】
    # ====================================================
    try:
        prompt = f"""
        你是一個親切、幽默的記帳助理「記帳米粒」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
        使用者輸入了：『{clean_text}』
        
        【分流任務】：
        1. 判定 intent (record, order_start, order_end, order_item, chat)。
        2. 如果對話中包含「花費與金額」（例如：今天買咖啡花了150元），請提取出紀錄 (intent="record")，並在 ai_reply 中給予親切的聊天回覆。
        3. 如果是純閒聊，intent="chat"，請在 ai_reply 陪使用者自然對話。
        4. 開團(order_start) 或 結單(order_end) 等控制指令，請在 ai_reply 給予親切的確認回覆。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.3),
        ).parsed

        # 1. AI 萃取記帳與陪聊
        if result.intent == "record":
            if result.records:
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category,
                            "timestamp": datetime.utcnow(), "created_by_uid": creator_id, "created_by_name": creator_name_str
                        })
                reply_text = result.ai_reply if result.ai_reply else f"✅ 已為您紀錄花費。"
                send_line_reply(reply_token, f"🤖 {reply_text}")

        # 2. 開團模式
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            db.collection("groups").document(target_id).update({"state": "order", "active_order_code": code_str, "order_items_temp": []})
            reply_text = result.ai_reply if result.ai_reply else f"🚀 【團購已啟動】本團單號：#{code_str}\n👉 請大家叫單時記得「@記帳米粒 品項 金額」喔！"
            send_line_reply(reply_token, reply_text)

        # 3. AI 萃取複雜點單與代點單
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                
                # 🎯 使用新的智慧 Tag 過濾機制
                real_tagged_ids = get_real_mentions(event)
                actual_buyer_id = real_tagged_ids[0] if real_tagged_ids else creator_id
                actual_buyer_name = resolve_id_to_name(target_id, actual_buyer_id)
                
                reply_lines = []
                for item in result.order_items:
                    clean_item_name = re.sub(r'@\S+', '', item.item_name).strip()
                    temp_items.append({
                        "buyer_id": actual_buyer_id, "buyer": actual_buyer_name,
                        "item": clean_item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()
                    })
                    reply_lines.append(f"📝 已接單：{clean_item_name} ${item.price}")
                    
                g_ref.update({"order_items_temp": temp_items})
                send_line_reply(reply_token, "\n".join(reply_lines))

        # 4. 截止結單
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            active_code = g_data.get("active_order_code", "")
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                total_amt = sum(i["price"] for i in temp_items)
                creator_name_str = resolve_id_to_name(target_id, creator_id)
                
                g_ref.collection("orders").document(f"{datetime.now().strftime('%Y%m%d')}_{active_code}").set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), "order_code": active_code, "total_amount": total_amt,
                    "master_payer_id": creator_id, "master_payer_name": creator_name_str, "items": temp_items, "timestamp": datetime.utcnow()
                })
                reply_text = result.ai_reply if result.ai_reply else f"🏁 【團購截止 ｜ 單號 #{active_code}】\n💰 總金額：${total_amt} 元\n💳 墊款：{creator_name_str}\n\n🤖 數據已更新！"
                send_line_reply(reply_token, reply_text)
            else:
                send_line_reply(reply_token, "🛑 因無人叫單，本團已直接關閉。")
                
            g_ref.update({"state": "normal", "order_items_temp": []})

        # 5. 純粹對話陪聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(reply_token, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "fast_regex_active", "version": "v11.0-Perfect-Name-Display"}
