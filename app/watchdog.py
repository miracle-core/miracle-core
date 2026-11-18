from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from app.models import (
    ClinicDoctor, AdCampaign, PaymentProof, TriageLog,
    OutreachLead, CampBooking, SosTrack, AppDevice
)
from app.telegram_bot import notify_owner


def build_number_report_text(db: Session) -> str:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    yest_start = today_start - timedelta(days=1)

    total_users = db.query(AppDevice).count()
    installed_users = db.query(AppDevice).filter(AppDevice.is_installed == True).count()
    yest_users = db.query(AppDevice).filter(
        AppDevice.last_seen_at >= yest_start, AppDevice.last_seen_at < today_start
    ).count()

    total_helped = db.query(TriageLog).count()
    yest_helped = db.query(TriageLog).filter(
        TriageLog.created_at >= yest_start, TriageLog.created_at < today_start
    ).count()

    total_sos = db.query(SosTrack).count()
    yest_sos = db.query(SosTrack).filter(
        SosTrack.updated_at >= yest_start, SosTrack.updated_at < today_start
    ).count()

    total_doctors = db.query(ClinicDoctor).count()
    verified_doctors = db.query(ClinicDoctor).filter(ClinicDoctor.is_verified == True).count()
    active_ads = db.query(AdCampaign).filter(AdCampaign.status == "active").count()

    total_camps = db.query(CampBooking).count()
    yest_camps = db.query(CampBooking).filter(
        CampBooking.created_at >= yest_start, CampBooking.created_at < today_start
    ).count()

    total_leads = db.query(OutreachLead).count()
    pending_pay = db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
    total_revenue = db.query(func.coalesce(func.sum(PaymentProof.amount), 0)).filter(
        PaymentProof.status == "approved"
    ).scalar() or 0

    return (
        f"📊 <b>MIRACLE Number-Wise Report</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"1️⃣ <b>Total Users / Installs:</b> {total_users} (App Installed: {installed_users})\n"
        f"2️⃣ <b>Yesterday Active Users:</b> {yest_users}\n"
        f"3️⃣ <b>Total Patients Helped:</b> {total_helped} (Yesterday: {yest_helped})\n"
        f"4️⃣ <b>Emergency SOS Provided:</b> {total_sos} (Yesterday: {yest_sos})\n"
        f"5️⃣ <b>Doctors / Clinics Added:</b> {total_doctors} (Verified: {verified_doctors})\n"
        f"6️⃣ <b>Active 10km Geo-Ads:</b> {active_ads}\n"
        f"7️⃣ <b>Free Camp Bookings:</b> {total_camps} (Yesterday: {yest_camps})\n"
        f"8️⃣ <b>CRM Sales Leads:</b> {total_leads}\n"
        f"9️⃣ <b>Pending Payment Approvals:</b> {pending_pay}\n"
        f"🔟 <b>Total Approved Revenue:</b> {total_revenue}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ System: <b>Healthy & Live</b>"
    )


async def run_watchdog(db: Session):
    now = datetime.utcnow()
    report = {
        "status": "ok",
        "checked_at": now.isoformat(),
        "db_alive": False,
        "expired_ads_closed": 0,
        "pending_payments": 0,
    }

    try:
        db.execute(text("SELECT 1"))
        report["db_alive"] = True
    except Exception as e:
        await notify_owner(f"🚨 <b>WATCHDOG CRITICAL:</b> Database down!\nError: <code>{str(e)[:200]}</code>")
        report["status"] = "db_error"
        return report

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

    report["pending_payments"] = (
        db.query(PaymentProof).filter(PaymentProof.status == "pending").count()
    )
    return report


async def send_daily_report(db: Session):
    msg = build_number_report_text(db)
    await notify_owner(msg)
    return {"sent": True}
