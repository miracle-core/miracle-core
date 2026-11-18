from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.models import ClinicDoctor, AdCampaign, PaymentProof, TriageLog, OutreachLead
from app.telegram_bot import notify_owner


async def run_watchdog(db: Session):
    now = datetime.utcnow()
    report = {
        "status": "ok",
        "checked_at": now.isoformat(),
        "db_alive": False,
        "expired_ads_closed": 0,
        "pending_payments": 0,
    }

    # 1. Database Watchdog
    try:
        db.execute(text("SELECT 1"))
        report["db_alive"] = True
    except Exception as e:
        await notify_owner(f"🚨 <b>WATCHDOG CRITICAL:</b> Database down!\nError: <code>{str(e)[:200]}</code>")
        report["status"] = "db_error"
        return report

    # 2. Auto-Expire Finished 10km Geo-Ads (Only alerts once when expired)
    expired_campaigns = (
        db.query(AdCampaign)
        .filter(AdCampaign.status == "active", AdCampaign.end_date <= now)
        .all()
    )

    for camp in expired_campaigns:
        camp.status = "expired"
        clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == camp.clinic_id).first()
        clinic_name = clinic.name if clinic else f"Clinic #{camp.clinic_id}"
        await notify_owner(
            f"⏰ <b>Ad Expired (Watchdog):</b>\n"
            f"🏥 {clinic_name}\n"
            f"📦 Plan: <code>{camp.plan_type}</code>\n"
            f"💡 Renewal follow-up bhejne ka time aa gaya hai!"
        )
        report["expired_ads_closed"] += 1

    if expired_campaigns:
        db.commit()

    # 3. Count Pending Payments
    report["pending_payments"] = (
        db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
    )

    return report


async def send_daily_report(db: Session):
    active_ads = db.query(AdCampaign).filter(AdCampaign.status == "active").count()
    pending_pay = db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
    total_clinics = db.query(ClinicDoctor).count()
    total_triage = db.query(TriageLog).count()
    total_leads = db.query(OutreachLead).count()

    await notify_owner(
        f"☀️ <b>MIRACLE Daily Watchdog Report</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📢 Active 10km Ads: <b>{active_ads}</b>\n"
        f"⏳ Pending Payment Approvals: <b>{pending_pay}</b>\n"
        f"🏥 Total Clinics: <b>{total_clinics}</b>\n"
        f"🩺 Total Patient Triages: <b>{total_triage}</b>\n"
        f"📩 Outreach Leads: <b>{total_leads}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ System Status: <b>Healthy & Running</b>"
    )
    return {"sent": True}
