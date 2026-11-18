import os
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import SystemConfig, UsageMetric, TriageLog, PaymentProof, AdCampaign, OutreachLead


DEFAULT_CONFIGS = {
    "ai_provider": "gemini_free",     # gemini_free | gemini_paid | openai
    "payment_mode": "local_bank",     # local_bank | payment_gateway
    "outreach_mode": "telegram_1tap", # telegram_1tap | whatsapp_cloud
}


def get_config(db: Session, key: str) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row:
        return row.value
    return DEFAULT_CONFIGS.get(key, "")


def set_config(db: Session, key: str, value: str):
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        row = SystemConfig(key=key, value=value)
        db.add(row)
    db.commit()


def record_metric(db: Session, feature: str, status_code: int = 200, latency_ms: int = 0, department: str = None):
    try:
        m = UsageMetric(
            feature=feature,
            department=department,
            status_code=status_code,
            latency_ms=latency_ms,
        )
        db.add(m)
        db.commit()
    except Exception:
        db.rollback()


async def run_strategist_analysis(db: Session, notify_fn):
    since = datetime.utcnow() - timedelta(hours=24)

    # 1. Crowd & Demand Hotspots (Last 24h)
    top_features = (
        db.query(UsageMetric.feature, func.count(UsageMetric.id).label("cnt"))
        .filter(UsageMetric.created_at >= since)
        .group_by(UsageMetric.feature)
        .order_by(func.count(UsageMetric.id).desc())
        .all()
    )
    top_depts = (
        db.query(TriageLog.detected_department, func.count(TriageLog.id).label("cnt"))
        .filter(TriageLog.created_at >= since)
        .group_by(TriageLog.detected_department)
        .order_by(func.count(TriageLog.id).desc())
        .limit(3)
        .all()
    )
    ai_errors = (
        db.query(UsageMetric)
        .filter(UsageMetric.created_at >= since, UsageMetric.status_code >= 429)
        .count()
    )
    total_requests = (
        db.query(UsageMetric).filter(UsageMetric.created_at >= since).count()
    )
    pending_payments = db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
    active_ads = db.query(AdCampaign).filter(AdCampaign.status == "active").count()

    current_ai = get_config(db, "ai_provider")
    current_pay = get_config(db, "payment_mode")
    current_outreach = get_config(db, "outreach_mode")

    recommendations = []
    buttons = []

    # 2. Detect Upgrade Needs & Ready Paid Keys
    if ai_errors >= 3 or total_requests >= 400:
        recommendations.append(
            "🔥 <b>AI Crowd/Limit Alert:</b> Free AI quota par pressure aa raha hai. "
            "Option: <b>Gemini Pay-As-You-Go</b> (~₹150-₹500/month) ya <b>OpenAI GPT-4o-mini</b> lagayein."
        )
    if os.getenv("OPENAI_API_KEY") and current_ai != "openai":
        recommendations.append("✅ <code>OPENAI_API_KEY</code> detected! Switch karne ke liye button dabayein:")
        buttons.append([{"text": "🔄 Switch AI -> OpenAI", "callback_data": "switch:ai_provider:openai"}])

    if (pending_payments >= 8 or active_ads >= 15) and current_pay == "local_bank":
        recommendations.append(
            "💳 <b>Payment Upgrade Alert:</b> Orders badh rahe hain! Manual screenshot check se bachne ke liye "
            "<b>Razorpay (India)</b> ya <b>Iyzico/Stripe (Turkey/Global)</b> lagayein."
        )
    if (os.getenv("RAZORPAY_KEY_ID") or os.getenv("STRIPE_SECRET_KEY")) and current_pay != "payment_gateway":
        recommendations.append("✅ Payment Gateway key detected! Auto-checkout on karein:")
        buttons.append([{"text": "🔄 Switch Payment -> Gateway", "callback_data": "switch:payment_mode:payment_gateway"}])

    if os.getenv("WHATSAPP_TOKEN") and current_outreach != "whatsapp_cloud":
        recommendations.append("✅ <code>WHATSAPP_TOKEN</code> detected! Full Auto WhatsApp Bot on karein:")
        buttons.append([{"text": "🔄 Switch Outreach -> Auto WhatsApp", "callback_data": "switch:outreach_mode:whatsapp_cloud"}])

    if total_requests >= 800:
        recommendations.append(
            "⚡ <b>Server Crowd Alert:</b> Traffic high hai! Zero-sleep speed ke liye <b>Render Starter ($7/mo)</b> par upgrade karein."
        )

    feat_lines = ", ".join([f"{f.feature} ({f.cnt})" for f in top_features]) or "Abhi data collect ho raha hai"
    dept_lines = ", ".join([f"{d[0]} ({d[1]})" for d in top_depts]) or "N/A"

    msg = (
        f"🧠 <b>Jarvis Strategist & Crowd Intelligence (24h)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 Total Requests: <b>{total_requests}</b> | AI Limit Hits: <b>{ai_errors}</b>\n"
        f"🔥 Top Crowd Features: {feat_lines}\n"
        f"🏥 Top Demand Depts: {dept_lines}\n"
        f"⚙️ Active Modes: AI=<code>{current_ai}</code> | Pay=<code>{current_pay}</code> | Outreach=<code>{current_outreach}</code>\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    if recommendations:
        msg += "💡 <b>Paid Upgrade & Switch Alerts:</b>\n" + "\n".join(recommendations)
    else:
        msg += "✅ Abhi sab Free Tier par smoothly chal raha hai. Paid upgrade ki zaroorat nahi!"

    reply_markup = {"inline_keyboard": buttons} if buttons else None
    await notify_fn(msg, reply_markup=reply_markup)

    return {
        "total_requests_24h": total_requests,
        "ai_errors_24h": ai_errors,
        "active_modes": {"ai": current_ai, "payment": current_pay, "outreach": current_outreach},
        "recommendations_count": len(recommendations),
    }
