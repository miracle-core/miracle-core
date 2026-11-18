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
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)


async def notify_owner(text: str, reply_markup: dict = None):
    await send_telegram_message(OWNER_ID, text, reply_markup)


async def handle_telegram_update(update: dict, db: Session):
    # 1. Handle Inline Button Click (Approve / Reject Payment)
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
                if campaign:
                    days = 30 if campaign.plan_type == "monthly_7500" else 7
                    campaign.status = "active"
                    campaign.start_date = datetime.utcnow()
                    campaign.end_date = datetime.utcnow() + timedelta(days=days)
                    clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == campaign.clinic_id).first()
                    if clinic:
                        clinic.is_verified = True
                db.commit()
                await notify_owner(f"✅ <b>Approved!</b> Order <code>{proof.order_code}</code> ab 10 km radius mein LIVE hai.")
        elif data.startswith("reject_pay:"):
            proof_id = int(data.split(":")[1])
            proof = db.query(PaymentProof).filter(PaymentProof.id == proof_id).first()
            if proof:
                proof.status = "rejected"
                db.commit()
                await notify_owner(f"❌ <b>Rejected:</b> Order <code>{proof.order_code}</code> cancel kar diya gaya.")
        return {"ok": True}

    # 2. Handle Text Commands from Owner
    msg = update.get("message")
    if not msg:
        return {"ok": True}

    sender_id = str(msg["from"]["id"])
    text = msg.get("text", "").strip()

    if sender_id != str(OWNER_ID):
        await send_telegram_message(sender_id, "⛔ Yeh MIRACLE Owner Command Center hai. Sirf authorized owner access kar sakta hai.")
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
                    f"Amount: ₹{p.amount}\nRef: {p.ai_extracted_ref or 'N/A'}",
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
        await notify_owner("Samajh gaya Ali ji! /stats ya /pending try karein.")

    return {"ok": True}
