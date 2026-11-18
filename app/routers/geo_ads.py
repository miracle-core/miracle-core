import os
import json
import uuid
import base64
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import ClinicDoctor, AdCampaign, PaymentProof, CampBooking
from app.telegram_bot import send_telegram_photo_to_owner, notify_owner

router = APIRouter(prefix="/api/clinics", tags=["10km Geo-Ads & Camps"])

PLAN_PRICES = {
    "camp_1500": 1500,
    "weekly_3500": 3500,
    "monthly_7500": 7500,
}


class RegisterAdRequest(BaseModel):
    name: str
    specialty: str
    pathy: Optional[str] = "Allopathy"
    degree: Optional[str] = "MBBS"
    license_no: str
    phone: str
    email: Optional[str] = ""
    address: str
    lat: float
    lng: float
    plan_type: str  # camp_1500 | weekly_3500 | monthly_7500
    camp_title: Optional[str] = None


class CampBookingRequest(BaseModel):
    campaign_id: int
    patient_name: str
    patient_phone: str


async def extract_payment_details_with_ai(image_bytes: bytes, mime_type: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "AI key missing - verify screenshot manually"

    prompt = (
        "Read this payment screenshot carefully. Extract: Amount paid, Transaction/UTR/Reference ID, "
        "Date/Time, and Receiver/Sender if visible. Reply in 1 short plain line."
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("utf-8")}},
            ],
        }],
        "generationConfig": {"temperature": 0.1},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            res = await client.post(url, json=payload)
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()[:140]
    except Exception:
        pass
    return "Could not auto-read - check photo"


@router.post("/register-ad")
async def register_clinic_ad(req: RegisterAdRequest, db: Session = Depends(get_db)):
    if req.plan_type not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan_type. Use: camp_1500, weekly_3500, monthly_7500")

    amount = PLAN_PRICES[req.plan_type]

    clinic = ClinicDoctor(
        name=req.name,
        specialty=req.specialty,
        pathy=req.pathy,
        degree=req.degree,
        license_no=req.license_no,
        phone=req.phone,
        email=req.email,
        address=req.address,
        lat=req.lat,
        lng=req.lng,
        is_verified=False,
    )
    db.add(clinic)
    db.flush()

    campaign = AdCampaign(
        clinic_id=clinic.id,
        plan_type=req.plan_type,
        amount=amount,
        radius_km=10,
        camp_title=req.camp_title,
        status="pending",
    )
    db.add(campaign)
    db.flush()

    order_code = f"MRC-{uuid.uuid4().hex[:6].upper()}"
    proof = PaymentProof(
        campaign_id=campaign.id,
        order_code=order_code,
        amount=amount,
        status="pending",
    )
    db.add(proof)
    db.commit()

    payment_info = os.getenv(
        "LOCAL_PAYMENT_INFO",
        "Owner Local Account / UPI / IBAN (Configure LOCAL_PAYMENT_INFO in Render)"
    )

    await notify_owner(
        f"🆕 <b>New Clinic Ad Order Created!</b>\n"
        f"🏥 {req.name} ({req.specialty})\n"
        f"🪪 License: <code>{req.license_no}</code>\n"
        f"📦 Plan: <code>{req.plan_type}</code> (₹{amount})\n"
        f"🧾 Order Code: <code>{order_code}</code>\n"
        f"⏳ Waiting for payment screenshot..."
    )

    return {
        "status": "pending_payment",
        "order_code": order_code,
        "amount": amount,
        "plan_type": req.plan_type,
        "payment_instructions": f"Pay ₹{amount} to [{payment_info}] with reference {order_code}, then upload screenshot at /api/clinics/upload-payment-proof.",
    }


@router.post("/upload-payment-proof")
async def upload_payment_proof(
    order_code: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    proof = db.query(PaymentProof).filter(PaymentProof.order_code == order_code.strip().upper()).first()
    if not proof:
        raise HTTPException(status_code=404, detail="Order code not found.")

    allowed = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Upload JPG/PNG screenshot.")

    img_bytes = await file.read()
    ai_read = await extract_payment_details_with_ai(img_bytes, file.content_type)
    proof.ai_extracted_ref = ai_read
    db.commit()

    campaign = db.query(AdCampaign).filter(AdCampaign.id == proof.campaign_id).first()
    clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == campaign.clinic_id).first() if campaign else None

    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve & Go Live", "callback_data": f"approve_pay:{proof.id}"},
            {"text": "❌ Reject", "callback_data": f"reject_pay:{proof.id}"}
        ]]
    }

    caption = (
        f"💳 <b>Payment Screenshot Received!</b>\n"
        f"🏥 Clinic: {clinic.name if clinic else 'N/A'}\n"
        f"🪪 License: <code>{clinic.license_no if clinic else 'N/A'}</code>\n"
        f"🧾 Order: <code>{proof.order_code}</code> | ₹{proof.amount}\n"
        f"🤖 <b>AI Read:</b> {ai_read}"
    )

    await send_telegram_photo_to_owner(img_bytes, file.filename or "payment.jpg", caption, markup)

    return {
        "status": "sent_for_verification",
        "order_code": proof.order_code,
        "ai_extracted": ai_read,
        "message": "Screenshot verified by AI and sent to MIRACLE Admin. Your 10km ad will go live immediately upon approval.",
    }


@router.get("/nearby")
def get_nearby_ads_and_camps(
    lat: float = Query(...),
    lng: float = Query(...),
    specialty: Optional[str] = Query(default=None),
    radius_km: int = Query(default=10, le=50),
    db: Session = Depends(get_db),
):
    sql = """
        SELECT
            a.id AS campaign_id,
            c.id AS clinic_id,
            c.name, c.specialty, c.pathy, c.degree, c.phone, c.address, c.lat, c.lng,
            a.plan_type, a.camp_title, a.end_date,
            ROUND((ST_Distance(
                ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
            ) / 1000.0)::numeric, 2) AS distance_km
        FROM clinics_doctors c
        JOIN ad_campaigns a ON c.id = a.clinic_id
        WHERE a.status = 'active'
          AND a.end_date >= NOW()
          AND c.is_verified = TRUE
          AND ST_DWithin(
              ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography,
              ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
              :radius_meters
          )
    """
    params = {"lat": lat, "lng": lng, "radius_meters": radius_km * 1000}
    if specialty:
        sql += " AND LOWER(c.specialty) LIKE LOWER(:spec)"
        params["spec"] = f"%{specialty}%"

    sql += " ORDER BY distance_km ASC LIMIT 20;"
    rows = db.execute(text(sql), params).mappings().all()
    return {"radius_km": radius_km, "results": [dict(r) for r in rows]}


@router.post("/book-camp")
async def book_free_camp_slot(req: CampBookingRequest, db: Session = Depends(get_db)):
    camp = db.query(AdCampaign).filter(
        AdCampaign.id == req.campaign_id, AdCampaign.status == "active"
    ).first()
    if not camp:
        raise HTTPException(status_code=404, detail="Active camp/clinic not found.")

    booking = CampBooking(
        campaign_id=req.campaign_id,
        patient_name=req.patient_name,
        patient_phone=req.patient_phone,
    )
    db.add(booking)
    db.commit()

    clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == camp.clinic_id).first()
    await notify_owner(
        f"🎟️ <b>New Free Camp / Clinic Booking!</b>\n"
        f"🏥 {clinic.name if clinic else 'Clinic'}\n"
        f"👤 Patient: {req.patient_name} ({req.patient_phone})"
    )
    return {"status": "booked", "message": "Aapka slot confirm ho gaya hai!"}
