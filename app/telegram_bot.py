import os
import json
from datetime import datetime, timedelta
import httpx
from sqlalchemy.orm import Session
from app.models import ClinicDoctor, AdCampaign, PaymentProof, TriageLog, OutreachLead, FamilyLink
from app.strategist_agent import (
    get_config, set_config, run_strategist_analysis
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.getenv("OWNER_TELEGRAM_ID", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_USERNAME_CACHE = os.getenv("TELEGRAM_BOT_USERNAME", "")


async def get_bot_username() -> str:
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE
    if not BOT_TOKEN:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{TG_API}/getMe")
        if r.status_code == 200:
            BOT_USERNAME_CACHE = r.json().get("result", {}).get("username", "")
    except Exception:
        pass
    return BOT_USERNAME_CACHE


async def send_telegram_message(chat_id: str, text: str, reply_markup: dict = None):
    if not BOT_TOKEN or not chat_id:
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)


async def send_telegram_photo_to_owner(
    image_bytes: bytes, filename: str, caption: str, reply_markup: dict = None
):
    if not BOT_TOKEN or not OWNER_ID:
        return
    data = {"chat_id": OWNER_ID, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"photo": (filename, image_bytes)}
    async with httpx.AsyncClient(timeout=25) as client:
        await client.post(f"{TG_API}/sendPhoto", data=data, files=files)


async def answer_callback(callback_query_id: str, text: str = "Done"):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{TG_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
        )


async def notify_owner(text: str, reply_markup: dict = None):
    await send_telegram_message(OWNER_ID, text, reply_markup)


async def handle_telegram_update(update: dict, db: Session):
    if "callback_query" in update:
        cb = update["callback_query"]
        sender_id = str(cb["from"]["id"])
        if sender_id != str(OWNER_ID):
            return {"ok": True}

        data = cb.get("data", "")

        # 1. Hot-Switch Paid Service Mode
        if data.startswith("switch:"):
            _, cfg_key, cfg_val = data.split(":", 2)
            set_config(db, cfg_key, cfg_val)
            await answer_callback(cb["id"], f"Switched to {cfg_val}!")
            await notify_owner(f"⚡ <b>Auto-Switched!</b> <code>{cfg_key}</code> ab <b>{cfg_val}</b> par chal raha hai.")
            return {"ok": True}

        # 2. Approve / Reject Clinic Payment
        if data.startswith("approve_pay:"):
            proof_id = int(data.split(":")[1])
            proof = db.query(PaymentProof).filter(PaymentProof.id == proof_id).first()
            if proof and proof.status == "pending":
                proof.status = "approved"
                campaign = db.query(AdCampaign).filter(AdCampaign.id == proof.campaign_id).first()
                clinic_name = "Clinic"
                if campaign:
                    days = 30 if campaign.plan_type == "monthly_7500" else 7
                    campaign.status = "active"
                    campaign.start_date = datetime.utcnow()
                    campaign.end_date = datetime.utcnow() + timedelta(days=days)
                    clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == campaign.clinic_id).first()
                    if clinic:
                        clinic.is_verified = True
                        clinic_name = clinic.name
                db.commit()
                await answer_callback(cb["id"], "Ad Live Ho Gaya!")
                await notify_owner(
                    f"✅ <b>Approved & LIVE!</b>\n🏥 {clinic_name}\n🧾 Order: <code>{proof.order_code}</code>"
                )
            else:
                await answer_callback(cb["id"], "Already processed")
        elif data.startswith("reject_pay:"):
            proof_id = int(data.split(":")[1])
            proof = db.query(PaymentProof).filter(PaymentProof.id == proof_id).first()
            if proof:
                proof.status = "rejected"
                db.commit()
                await answer_callback(cb["id"], "Rejected")
                await notify_owner(f"❌ <b>Rejected:</b> Order <code>{proof.order_code}</code> cancel.")
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    sender_id = str(msg["from"]["id"])
    text = msg.get("text", "").strip()

    if sender_id != str(OWNER_ID):
        if text.startswith("/start FAM-"):
            fcode = text.split(" ", 1)[1].strip().upper()
            rel_name = msg["from"].get("first_name", "Family Member")
            existing = db.query(FamilyLink).filter(
                FamilyLink.family_code == fcode,
                FamilyLink.relative_tg_id == sender_id
            ).first()
            seed_row = db.query(FamilyLink).filter(FamilyLink.family_code == fcode).first()
            p_name = seed_row.patient_name if seed_row else "Your family member"
            if not existing:
                db.add(FamilyLink(
                    family_code=fcode,
                    patient_name=p_name,
                    relative_tg_id=sender_id,
                    relative_name=rel_name
                ))
                db.commit()
            await send_telegram_message(
                sender_id,
                f"✅ <b>Emergency Family Alert Activated!</b>\n\n"
                f"Namaste <b>{rel_name}</b>, <b>{p_name}</b> ne aapko MIRACLE Health App mein apna "
                f"<b>Emergency Contact</b> banaya hai.\n\n"
                f"Jab bhi woh SOS dabayenge, aapko turant yahan unka <b>Live Moving GPS Location & Blood Group</b> milega. "
                f"Aapko kuch aur karne ki zaroorat nahi hai! 🩺❤️"
            )
            return {"ok": True}

        await send_telegram_message(
            sender_id,
            f"🩺 <b>MIRACLE Emergency Family Alert</b>\n\n"
            f"Aapka <b>Family SOS ID</b> hai: <code>{sender_id}</code>\n"
            f"(Agar kisi parivar wale ne aapko invite link bheja hai, toh us link par click karke Start karein)."
        )
        return {"ok": True}

    if text.startswith("/start") or text.startswith("/help"):
        await notify_owner(
            "🤖 <b>MIRACLE • Ali Vepa AI Command Center</b>\n\n"
            "/stats - Aaj ka live data\n"
            "/strategist - Crowd & Paid Upgrade Report\n"
            "/switches - AI / Payment / WhatsApp switch karein\n"
            "/pending - Pending payment approvals\n"
            "/ads - Active 10 km geo-ads"
        )
    elif text.startswith("/strategist"):
        await run_strategist_analysis(db, notify_owner)
    elif text.startswith("/switches"):
        markup = {
            "inline_keyboard": [
                [
                    {"text": "AI: Gemini Free/Paid", "callback_data": "switch:ai_provider:gemini_paid"},
                    {"text": "AI: OpenAI", "callback_data": "switch:ai_provider:openai"},
                ],
                [
                    {"text": "Pay: Local Bank", "callback_data": "switch:payment_mode:local_bank"},
                    {"text": "Pay: Gateway", "callback_data": "switch:payment_mode:payment_gateway"},
                ],
                [
                    {"text": "WA: 1-Tap Link", "callback_data": "switch:outreach_mode:telegram_1tap"},
                    {"text": "WA: Auto Cloud", "callback_data": "switch:outreach_mode:whatsapp_cloud"},
                ],
            ]
        }
        await notify_owner(
            f"⚙️ <b>Current System Switches:</b>\n"
            f"• AI: <code>{get_config(db, 'ai_provider')}</code>\n"
            f"• Payment: <code>{get_config(db, 'payment_mode')}</code>\n"
            f"• Outreach: <code>{get_config(db, 'outreach_mode')}</code>\n\n"
            f"Switch karne ke liye neeche button dabayein:",
            reply_markup=markup,
        )
    elif text.startswith("/stats"):
        from app.watchdog import build_number_report_text
        await notify_owner(build_number_report_text(db))
    elif text.startswith("/pending"):
        pendings = db.query(PaymentProof).filter(PaymentProof.status == "pending").all()
        if not pendings:
            await notify_owner("✅ Koi pending payment approval nahi hai.")
        else:
            for p in pendings:
                markup = {
                    "inline_keyboard": [[
                        {"text": "✅ Approve & Go Live", "callback_data": f"approve_pay:{p.id}"},
                        {"text": "❌ Reject", "callback_data": f"reject_pay:{p.id}"}
                    ]]
                }
                await notify_owner(
                    f"🔔 <b>Pending Order:</b> <code>{p.order_code}</code> | ₹{p.amount}\nRef: {p.ai_extracted_ref or 'N/A'}",
                    reply_markup=markup
                )
    elif text.startswith("/ads"):
        actives = db.query(AdCampaign).filter(AdCampaign.status == "active").all()
        if not actives:
            await notify_owner("Abhi koi active ad nahi hai.")
        else:
            lines = [f"• ID #{a.id} | {a.plan_type} | Ends: {a.end_date.strftime('%d-%b')}" for a in actives]
            await notify_owner("📢 <b>Active Ads:</b>\n" + "\n".join(lines))
    else:
        # Agar owner "switch" ya "paid" likh kar bheje toh strategist auto-check karega
        if any(w in text.lower() for w in ["switch", "paid", "upgrade", "crowd", "status"]):
            await run_strategist_analysis(db, notify_owner)
        else:
            await notify_owner("Samajh gaya Ali ji! /strategist, /switches ya /stats try karein.")

    return {"ok": True}
