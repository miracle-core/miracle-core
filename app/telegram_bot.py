import os
from datetime import datetime, timedelta
import httpx
from sqlalchemy.orm import Session
from app.models import ClinicDoctor, AdCampaign, PaymentProof, TriageLog, OutreachLead

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = os.getenv("OWNER_TELEGRAM_ID", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


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
    import json
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
                    f"✅ <b>Approved & LIVE!</b>\n"
                    f"🏥 {clinic_name}\n"
                    f"🧾 Order: <code>{proof.order_code}</code>\n"
                    f"📍 10 km radius mein ab patients ko dikhega!"
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
                await notify_owner(f"❌ <b>Rejected:</b> Order <code>{proof.order_code}</code> cancel kar diya gaya.")
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    sender_id = str(msg["from"]["id"])
    text = msg.get("text", "").strip()

    if sender_id != str(OWNER_ID):
        await send_telegram_message(sender_id, "⛔ Yeh MIRACLE Owner Command Center hai.")
        return {"ok": True}

    if text.startswith("/start") or text.startswith("/help"):
        await notify_owner(
            "🤖 <b>MIRACLE Jarvis Command Center Active!</b>\n\n"
            "Commands:\n"
            "/stats - Aaj ka live data\n"
            "/pending - Pending payment approvals\n"
            "/ads - Active 10 km geo-ads list"
        )
    elif text.startswith("/stats"):
        total_clinics = db.query(ClinicDoctor).count()
        active_ads = db.query(AdCampaign).filter(AdCampaign.status == "active").count()
        pending_pay = db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
        total_triage = db.query(TriageLog).count()
        total_leads = db.query(OutreachLead).count()
        await notify_owner(
            f"📊 <b>MIRACLE Live Stats</b>\n"
            f"🏥 Registered Clinics: <b>{total_clinics}</b>\n"
            f"📢 Active 10km Ads: <b>{active_ads}</b>\n"
            f"⏳ Pending Payments: <b>{pending_pay}</b>\n"
            f"🩺 Patient Triage Checks: <b>{total_triage}</b>\n"
            f"📩 Sales Leads: <b>{total_leads}</b>"
        )
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
                    f"🔔 <b>Pending Order:</b> <code>{p.order_code}</code>\n"
                    f"Amount: ₹{p.amount}\nAI Read Ref: {p.ai_extracted_ref or 'Screenshot pending'}",
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
        await notify_owner("Samajh gaya Ali ji! /stats, /pending ya /ads try karein.")

    return {"ok": True}
