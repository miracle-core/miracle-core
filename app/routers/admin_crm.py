import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ClinicDoctor, AdCampaign, PaymentProof, OutreachLead, CampBooking, TriageLog

router = APIRouter(prefix="/api/admin", tags=["Owner Mini CRM"])


def verify_admin(x_admin_secret: str = Header(default=""), secret: str = ""):
    expected = os.getenv("CRON_SECRET", "miracle_cron_2025")
    if x_admin_secret != expected and secret != expected:
        raise HTTPException(status_code=403, detail="Invalid Admin Secret")


@router.get("/dashboard")
def get_crm_dashboard(
    secret: str = "",
    x_admin_secret: str = Header(default=""),
    db: Session = Depends(get_db),
):
    verify_admin(x_admin_secret, secret)

    clinics = db.query(ClinicDoctor).order_by(ClinicDoctor.id.desc()).limit(50).all()
    campaigns = db.query(AdCampaign).order_by(AdCampaign.id.desc()).limit(50).all()
    payments = db.query(PaymentProof).order_by(PaymentProof.id.desc()).limit(50).all()
    leads = db.query(OutreachLead).order_by(OutreachLead.id.desc()).limit(80).all()
    bookings = db.query(CampBooking).order_by(CampBooking.id.desc()).limit(50).all()
    total_triages = db.query(TriageLog).count()

    clinic_map = {c.id: c for c in clinics}
    camp_map = {a.id: a for a in campaigns}

    return {
        "summary": {
            "total_clinics": len(clinics),
            "active_ads": sum(1 for a in campaigns if a.status == "active"),
            "pending_payments": sum(1 for p in payments if p.status == "pending"),
            "total_leads": len(leads),
            "camp_bookings": len(bookings),
            "total_triages": total_triages,
        },
        "payments": [
            {
                "id": p.id,
                "order_code": p.order_code,
                "amount": p.amount,
                "status": p.status,
                "ai_read": p.ai_extracted_ref,
                "clinic_name": clinic_map[camp_map[p.campaign_id].clinic_id].name
                if p.campaign_id in camp_map and camp_map[p.campaign_id].clinic_id in clinic_map
                else "Clinic",
            }
            for p in payments
        ],
        "leads": [
            {
                "id": l.id,
                "clinic_name": l.clinic_name,
                "specialty": l.specialty,
                "city": l.city,
                "phone": l.phone,
                "email": l.email,
                "stage": l.stage,
            }
            for l in leads
        ],
        "bookings": [
            {
                "id": b.id,
                "campaign_id": b.campaign_id,
                "patient_name": b.patient_name,
                "patient_phone": b.patient_phone,
                "created_at": b.created_at.strftime("%d-%b %H:%M") if b.created_at else "",
            }
            for b in bookings
        ],
    }


@router.post("/approve-payment/{proof_id}")
def admin_approve_payment(
    proof_id: int,
    secret: str = "",
    x_admin_secret: str = Header(default=""),
    db: Session = Depends(get_db),
):
    verify_admin(x_admin_secret, secret)
    proof = db.query(PaymentProof).filter(PaymentProof.id == proof_id).first()
    if not proof:
        raise HTTPException(status_code=404, detail="Payment not found")
    proof.status = "approved"
    camp = db.query(AdCampaign).filter(AdCampaign.id == proof.campaign_id).first()
    if camp:
        days = 30 if camp.plan_type == "monthly_7500" else 7
        camp.status = "active"
        camp.start_date = datetime.utcnow()
        camp.end_date = datetime.utcnow() + timedelta(days=days)
        clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == camp.clinic_id).first()
        if clinic:
            clinic.is_verified = True
    db.commit()
    return {"status": "approved"}
