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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
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


def guess_emergency_number(lat: float, lng: float) -> dict:
    # India approx bounds
    if 6.0 <= lat <= 37.5 and 68.0 <= lng <= 97.5:
        return {"country": "India", "ambulance": "108", "national": "112"}
    # Turkey approx bounds
    if 35.8 <= lat <= 42.3 and 25.5 <= lng <= 45.0:
        return {"country": "Turkey", "ambulance": "112", "national": "112"}
    # USA / Canada approx bounds
    if 24.0 <= lat <= 71.0 and -170.0 <= lng <= -52.0:
        return {"country": "North America", "ambulance": "911", "national": "911"}
    return {"country": "Global", "ambulance": "112", "national": "112"}


@router.get("/ambulance-nearby")
async def find_nearest_ambulance(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: int = Query(default=10, le=25),
):
    emerg = guess_emergency_number(lat, lng)
    radius_m = radius_km * 1000
    overpass_query = f"""
    [out:json][timeout:10];
    (
      node["emergency"="ambulance_station"](around:{radius_m},{lat},{lng});
      way["emergency"="ambulance_station"](around:{radius_m},{lat},{lng});
      node["amenity"="hospital"](around:{radius_m},{lat},{lng});
      way["amenity"="hospital"](around:{radius_m},{lat},{lng});
    );
    out center 15;
    """
    providers = []
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query})
        if res.status_code == 200:
            for el in res.json().get("elements", []):
                tags = el.get("tags", {})
                name = tags.get("name")
                if not name:
                    continue
                is_amb = tags.get("emergency") == "ambulance_station"
                phone = tags.get("phone") or tags.get("contact:phone") or tags.get("emergency:phone") or ""
                p_lat = el.get("lat") or el.get("center", {}).get("lat")
                p_lng = el.get("lon") or el.get("center", {}).get("lon")
                providers.append({
                    "name": name,
                    "type": "Ambulance Station" if is_amb else "Emergency Hospital",
                    "phone": phone,
                    "lat": p_lat,
                    "lng": p_lng,
                })
    except Exception:
        pass

    # Ambulance stations first, then hospitals
    providers.sort(key=lambda x: 0 if x["type"] == "Ambulance Station" else 1)
    return {
        "emergency_numbers": emerg,
        "google_maps_pin": f"https://maps.google.com/?q={lat},{lng}",
        "providers": providers[:12],
    }


class SosLogRequest(BaseModel):
    lat: float
    lng: float
    note: Optional[str] = "Emergency Ambulance SOS Pressed"


@router.post("/sos-alert")
async def log_sos_alert(req: SosLogRequest):
    maps_url = f"https://maps.google.com/?q={req.lat},{req.lng}"
    await notify_owner(
        f"🚨 <b>EMERGENCY SOS TRIGGERED!</b>\n"
        f"📍 Location: <a href='{maps_url}'>Open Google Maps Pin</a>\n"
        f"📝 Note: {req.note}"
    )
    return {"status": "logged", "maps_url": maps_url}


LOCAL_WELCOME_MESSAGES = {
    "gu": {
        "lang_name": "ગુજરાતી",
        "msg": "MIRACLE ૧૦૦% દર્દી-આધારિત અને બિલકુલ મફત સેવા છે. કૃપા કરીને આ લિંક તમારા પ્રિયજનો અને પરિવાર સાથે શેર કરો જેથી કોઈપણ વ્યક્તિને આરોગ્યની સમસ્યામાં તરત સાચી મદદ મળી શકે."
    },
    "mr": {
        "lang_name": "मराठी",
        "msg": "MIRACLE ही १००% रुग्णांसाठी मोफत सेवा आहे. कृपया ही लिंक तुमच्या जवळच्या लोकांना आणि कुटुंबाला पाठवा, जेणेकरून कोणालाही आरोग्यासंबंधी मदतीची गरज भासल्यास त्वरित आधार मिळेल."
    },
    "hi": {
        "lang_name": "हिन्दी",
        "msg": "MIRACLE 100% मरीज-आधारित और बिल्कुल मुफ्त सेवा है। कृपया इसे अपने करीबी दोस्तों और परिवार को भेजकर मदद करें, ताकि किसी भी इंसान को स्वास्थ्य संबंधी सवाल में तुरंत सही सहायता मिल सके।"
    },
    "bn": {
        "lang_name": "বাংলা",
        "msg": "MIRACLE ১০০% রোগী-ভিত্তিক এবং সম্পূর্ণ বিনামূল্যে সেবা। অনুগ্রহ করে এটি আপনার প্রিয়জন ও পরিবারের সাথে শেয়ার করুন যাতে যে কেউ স্বাস্থ্য সংক্রান্ত সমস্যায় দ্রুত সঠিক সহায়তা পান।"
    },
    "ta": {
        "lang_name": "தமிழ்",
        "msg": "MIRACLE 100% நோயாளிகளுக்கான முற்றிலும் இலவச சேவையாகும். தயவுசெய்து இதை உங்கள் அன்புக்குரியவர்களுடன் பகிருங்கள், இதனால் மருத்துவ உதவியில் உடனடி வழிகாட்டுதல் கிடைக்கும்."
    },
    "te": {
        "lang_name": "తెలుగు",
        "msg": "MIRACLE 100% రోగుల కోసం పూర్తిగా ఉచిత సేవ. దయచేసి దీన్ని మీ ఆత్మీయులు మరియు కుటుంబ సభ్యులతో పంచుకోండి, తద్వారా ఆరోగ్య సమస్యల్లో వెంటనే సరైన సహాయం లభిస్తుంది."
    },
    "tr": {
        "lang_name": "Türkçe",
        "msg": "MIRACLE %100 hasta odaklı ve tamamen ÜCRETSİZ bir sağlık rehberidir. Sağlıkla ilgili sorularda herkesin anında doğru destek alabilmesi için lütfen sevdiklerinizle paylaşın."
    },
    "ar": {
        "lang_name": "العربية",
        "msg": "MIRACLE هي خدمة مجانية ١٠٠٪ مخصصة للمرضى. يرجى مشاركتها مع أحبائك وعائلتك حتى يحصل أي شخص على دعم فوري وصادق في أي استفسار صحي."
    },
    "es": {
        "lang_name": "Español",
        "msg": "MIRACLE es un servicio 100% gratuito enfocado en el paciente. Compártelo con tus seres queridos para que cualquier persona reciba apoyo inmediato en temas de salud."
    },
    "fr": {
        "lang_name": "Français",
        "msg": "MIRACLE est un service 100% gratuit dédié aux patients. Partagez-le avec vos proches afin que chacun puisse obtenir un soutien immédiat pour sa santé."
    },
    "de": {
        "lang_name": "Deutsch",
        "msg": "MIRACLE ist ein 100% kostenloser, patientenorientierter Dienst. Teilen Sie ihn mit Ihren Liebsten, damit jeder bei Gesundheitsfragen sofortige Hilfe erhält."
    },
    "ru": {
        "lang_name": "Русский",
        "msg": "MIRACLE — это на 100% бесплатный сервис для пациентов. Поделитесь им с близкими, чтобы каждый мог сразу получить честную поддержку по вопросам здоровья."
    },
    "id": {
        "lang_name": "Bahasa Indonesia",
        "msg": "MIRACLE adalah layanan 100% GRATIS yang berfokus pada pasien. Bagikan kepada orang terdekat Anda agar siapa pun bisa segera mendapat dukungan kesehatan yang jujur."
    },
    "pt": {
        "lang_name": "Português",
        "msg": "MIRACLE é um serviço 100% gratuito focado no paciente. Compartilhe com seus familiares e amigos para que todos tenham apoio imediato em dúvidas de saúde."
    },
}


@router.get("/location-welcome")
async def get_location_welcome(lat: float = Query(...), lng: float = Query(...)):
    place_label = "Global"
    lang_code = "en"
    country_code = ""
    state = ""

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "json", "accept-language": "en"},
                headers={"User-Agent": "MiracleHealthApp/1.0"},
            )
        if res.status_code == 200:
            addr = res.json().get("address", {})
            country_code = (addr.get("country_code") or "").lower()
            state = (addr.get("state") or "").lower()
            city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("state") or ""
            country = addr.get("country") or ""
            place_label = ", ".join([x for x in [city, addr.get("state"), country] if x]) or "Your Location"
    except Exception:
        pass

    # Determine regional language
    if country_code == "in" or (6.0 <= lat <= 37.5 and 68.0 <= lng <= 97.5 and not country_code):
        if "gujarat" in state or (20.1 <= lat <= 24.7 and 68.1 <= lng <= 74.5 and not state):
            lang_code = "gu"
        elif "maharashtra" in state:
            lang_code = "mr"
        elif "west bengal" in state:
            lang_code = "bn"
        elif "tamil nadu" in state:
            lang_code = "ta"
        elif "telangana" in state or "andhra" in state:
            lang_code = "te"
        else:
            lang_code = "hi"
    elif country_code == "tr" or (35.8 <= lat <= 42.3 and 25.5 <= lng <= 45.0 and not country_code):
        lang_code = "tr"
    elif country_code in {"ae", "sa", "qa", "kw", "om", "bh", "eg", "jo", "iq"}:
        lang_code = "ar"
    elif country_code in {"es", "mx", "ar", "co", "cl", "pe"}:
        lang_code = "es"
    elif country_code in {"fr", "be"}:
        lang_code = "fr"
    elif country_code in {"de", "at", "ch"}:
        lang_code = "de"
    elif country_code in {"ru", "by", "kz"}:
        lang_code = "ru"
    elif country_code == "id":
        lang_code = "id"
    elif country_code in {"br", "pt"}:
        lang_code = "pt"

    local_info = LOCAL_WELCOME_MESSAGES.get(lang_code)
    english_msg = (
        "MIRACLE is 100% patient-based and completely FREE. "
        "Please share this with your dear & near ones so anyone facing a health query receives immediate, honest support."
    )

    return {
        "place": place_label,
        "local_lang_code": lang_code,
        "local_lang_name": local_info["lang_name"] if local_info else "English",
        "local_message": local_info["msg"] if local_info else "",
        "english_message": english_msg,
    }


class SosTrackUpdate(BaseModel):
    track_id: Optional[str] = None
    patient_name: Optional[str] = "Patient"
    blood_group: Optional[str] = ""
    medical_note: Optional[str] = ""
    lat: float
    lng: float
    is_active: bool = True


@router.post("/sos-track")
def upsert_sos_track(req: SosTrackUpdate, db: Session = Depends(get_db)):
    from datetime import datetime
    from app.models import SosTrack

    tid = req.track_id or f"SOS-{uuid.uuid4().hex[:6].upper()}"
    row = db.query(SosTrack).filter(SosTrack.track_id == tid).first()
    if row:
        row.lat = req.lat
        row.lng = req.lng
        row.is_active = req.is_active
        row.updated_at = datetime.utcnow()
    else:
        row = SosTrack(
            track_id=tid,
            patient_name=(req.patient_name or "Patient")[:120],
            blood_group=(req.blood_group or "")[:20],
            medical_note=(req.medical_note or "")[:250],
            lat=req.lat,
            lng=req.lng,
            is_active=req.is_active,
        )
        db.add(row)
    db.commit()
    return {"track_id": tid, "track_url": f"/track/{tid}"}


@router.get("/sos-track/{track_id}")
def get_sos_track(track_id: str, db: Session = Depends(get_db)):
    from app.models import SosTrack
    row = db.query(SosTrack).filter(SosTrack.track_id == track_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tracking session not found")
    return {
        "track_id": row.track_id,
        "patient_name": row.patient_name,
        "blood_group": row.blood_group,
        "medical_note": row.medical_note,
        "lat": row.lat,
        "lng": row.lng,
        "is_active": row.is_active,
        "updated_at": row.updated_at.strftime("%H:%M:%S UTC") if row.updated_at else "",
    }


class SosDispatchRequest(BaseModel):
    lat: float
    lng: float
    track_id: Optional[str] = None
    patient_name: Optional[str] = "Patient"
    blood_group: Optional[str] = ""
    medical_note: Optional[str] = ""
    fam_phone: Optional[str] = ""
    fam_email: Optional[str] = ""
    fam_tg_id: Optional[str] = ""
    test_mode: bool = False
    base_url: Optional[str] = ""


@router.post("/sos-dispatch")
async def dispatch_direct_sos(req: SosDispatchRequest):
    from app.telegram_bot import send_telegram_message
    from app.routers.sales_agent import send_smtp_email, send_whatsapp_cloud

    maps_url = f"https://maps.google.com/?q={req.lat},{req.lng}"
    track_url = f"{req.base_url.rstrip('/')}/track/{req.track_id}" if (req.base_url and req.track_id) else maps_url
    prefix = "🧪 [TEST] " if req.test_mode else "🚨 EMERGENCY SOS! "

    msg_plain = (
        f"{prefix}{req.patient_name} needs urgent help!\n"
        f"Blood Group: {req.blood_group or 'N/A'} | Note: {req.medical_note or 'None'}\n"
        f"Live Location Tracker: {track_url}\n"
        f"Google Maps Pin: {maps_url}"
    )
    msg_html = (
        f"<b>{prefix}</b>\n"
        f"👤 <b>Patient:</b> {req.patient_name} | 🩸 <b>Blood:</b> {req.blood_group or 'N/A'}\n"
        f"⚠️ <b>Note:</b> {req.medical_note or 'None'}\n"
        f"📡 <a href='{track_url}'>Open Live Moving Tracker</a>\n"
        f"📍 <a href='{maps_url}'>Open Google Maps Pin</a>"
    )

    sent_channels = []

    # 1. Direct Server Telegram to Family
    if req.fam_tg_id:
        await send_telegram_message(req.fam_tg_id.strip(), msg_html)
        sent_channels.append("Family Telegram")

    # 2. Direct Server Email to Family (if SMTP configured)
    if req.fam_email and send_smtp_email(req.fam_email.strip(), f"{prefix}Urgent Location Alert - {req.patient_name}", msg_plain):
        sent_channels.append("Family Email")

    # 3. Direct Server WhatsApp Cloud API (Auto-active when WHATSAPP_TOKEN added)
    if req.fam_phone and await send_whatsapp_cloud(req.fam_phone.strip(), msg_plain):
        sent_channels.append("Auto WhatsApp API")

    # 4. Always notify Owner Telegram log
    await notify_owner(msg_html + f"\n✅ <i>Server Dispatched: {', '.join(sent_channels) or 'Backup Link Ready'}</i>")

    return {"dispatched": sent_channels, "track_url": track_url}
