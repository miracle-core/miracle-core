import os
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OutreachLead, AdCampaign, ClinicDoctor
from app.telegram_bot import notify_owner

router = APIRouter(prefix="/api/sales", tags=["AI Sales Agent & Auto Discovery"])

GEMINI_MODEL = "gemini-3.8-flash"


class DiscoverRequest(BaseModel):
    lat: float
    lng: float
    city_name: str
    radius_km: int = 10


async def generate_ai_text(prompt: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return ""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            res = await client.post(url, json=payload)
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        pass
    return ""


def send_smtp_email(to_email: str, subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not (host and user and password and to_email):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_email
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True
    except Exception:
        return False


async def send_whatsapp_cloud(phone: str, text_msg: str) -> bool:
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_ID", "").strip()
    if not (token and phone_id and phone):
        return False
    clean_phone = "".join(ch for ch in phone if ch.isdigit())
    if len(clean_phone) == 10:
        clean_phone = "91" + clean_phone
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": text_msg},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(url, headers=headers, json=payload)
        return res.status_code in (200, 201)
    except Exception:
        return False


@router.post("/discover-clinics")
async def discover_real_clinics(req: DiscoverRequest, db: Session = Depends(get_db)):
    radius_m = min(req.radius_km, 25) * 1000
    overpass_query = f"""
    [out:json][timeout:25];
    (
      node["amenity"~"hospital|clinic|doctors|dentist|pharmacy"](around:{radius_m},{req.lat},{req.lng});
      way["amenity"~"hospital|clinic|doctors|dentist|pharmacy"](around:{radius_m},{req.lat},{req.lng});
    );
    out center 40;
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": overpass_query},
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Map API timeout: {str(e)}")

    if res.status_code != 200:
        raise HTTPException(status_code=502, detail="OpenStreetMap busy, try again in 1 minute.")

    elements = res.json().get("elements", [])
    added = []
    public_preview = []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        amenity = tags.get("amenity", "clinic").title()
        specialty = tags.get("healthcare:speciality", amenity).replace("_", " ").title()
        phone = tags.get("phone") or tags.get("contact:phone") or ""
        email = tags.get("email") or tags.get("contact:email") or ""
        c_lat = el.get("lat") or el.get("center", {}).get("lat")
        c_lng = el.get("lon") or el.get("center", {}).get("lon")

        public_preview.append({
            "name": name,
            "category": amenity,
            "specialty": specialty,
            "phone": phone,
            "lat": c_lat,
            "lng": c_lng,
        })

        exists = (
            db.query(OutreachLead)
            .filter(OutreachLead.clinic_name == name, OutreachLead.city == req.city_name)
            .first()
        )
        if not exists:
            lead = OutreachLead(
                clinic_name=name[:200],
                specialty=specialty[:120],
                phone=phone[:40] if phone else None,
                email=email[:150] if email else None,
                city=req.city_name[:100],
                stage="new",
                next_followup_at=datetime.utcnow(),
            )
            db.add(lead)
            added.append(name)

    db.commit()

    await notify_owner(
        f"🗺️ <b>Auto-Discovery Complete ({req.city_name})!</b>\n"
        f"🔍 Found on Map: <b>{len(public_preview)}</b> clinics/hospitals\n"
        f"🆕 Added to CRM Leads: <b>{len(added)}</b>"
    )

    return {
        "city": req.city_name,
        "total_found_on_map": len(public_preview),
        "new_leads_saved_to_crm": len(added),
        "clinics": public_preview[:20],
    }


@router.get("/public-nearby")
async def get_public_nearby_hospitals(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: int = Query(default=10, le=25),
):
    """Unbiased fallback: Shows real OpenStreetMap hospitals/clinics even if no paid ads exist yet."""
    radius_m = radius_km * 1000
    overpass_query = f"""
    [out:json][timeout:20];
    (
      node["amenity"~"hospital|clinic|doctors|dentist"](around:{radius_m},{lat},{lng});
      way["amenity"~"hospital|clinic|doctors|dentist"](around:{radius_m},{lat},{lng});
    );
    out center 25;
    """
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            res = await client.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query})
        if res.status_code != 200:
            return {"results": []}
        results = []
        for el in res.json().get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name")
            if not name:
                continue
            results.append({
                "name": name,
                "type": tags.get("amenity", "clinic"),
                "phone": tags.get("phone") or tags.get("contact:phone") or "",
                "lat": el.get("lat") or el.get("center", {}).get("lat"),
                "lng": el.get("lon") or el.get("center", {}).get("lon"),
                "source": "OpenStreetMap (Unpaid Public Listing)",
            })
        return {"radius_km": radius_km, "results": results}
    except Exception:
        return {"results": []}


@router.post("/run-outreach")
async def run_ai_outreach_batch(limit: int = 5, db: Session = Depends(get_db)):
    now = datetime.utcnow()
    leads = (
        db.query(OutreachLead)
        .filter(
            OutreachLead.stage.in_(["new", "pitched", "followup_1"]),
            (OutreachLead.next_followup_at == None) | (OutreachLead.next_followup_at <= now),
        )
        .limit(limit)
        .all()
    )

    if not leads:
        return {"processed": 0, "message": "No leads due for outreach right now."}

    results = []
    wa_links_for_owner = []

    for lead in leads:
        is_followup = lead.stage != "new"
        pitch_prompt = (
            f"Write a short, respectful, honest outreach message in simple Hinglish for '{lead.clinic_name}' "
            f"(Specialty: {lead.specialty or 'Healthcare'}, City: {lead.city or ''}). "
            f"Message Type: {'Gentle Follow-up' if is_followup else 'First Introduction'}. "
            f"Explain MIRACLE Health App: Patients get unbiased second opinions & report clarity, and we connect them "
            f"to verified clinics within a 10 km radius with ZERO patient commission. "
            f"Plans: Free Health Camp Ad ₹1,500/week, Weekly Spotlight ₹3,500, Monthly Growth ₹7,500. "
            f"Keep it under 70 words, warm, non-pushy."
        )
        pitch_msg = await generate_ai_text(pitch_prompt)
        if not pitch_msg:
            pitch_msg = (
                f"Namaste {lead.clinic_name} team! MIRACLE App par aapke 10 km area ke patients sahi doctor "
                f"dhoondh rahe hain. Zero commission par apna Verified Clinic Ad (₹3,500/wk, ₹7,500/mo) ya "
                f"Free Camp Ad (₹1,500/wk) lagane ke liye reply karein."
            )

        email_sent = False
        wa_sent = False

        if lead.email:
            email_sent = send_smtp_email(
                lead.email,
                f"Partner with MIRACLE - Reach Patients Within 10km of {lead.clinic_name}",
                pitch_msg,
            )

        if lead.phone:
            wa_sent = await send_whatsapp_cloud(lead.phone, pitch_msg)
            if not wa_sent:
                digits = "".join(c for c in lead.phone if c.isdigit())
                if len(digits) == 10:
                    digits = "91" + digits
                if len(digits) >= 10:
                    wa_url = f"https://wa.me/{digits}?text={quote(pitch_msg)}"
                    wa_links_for_owner.append(f"• <b>{lead.clinic_name}</b>: <a href='{wa_url}'>Click to Send WhatsApp</a>")

        # Advance CRM Stage
        if lead.stage == "new":
            lead.stage = "pitched"
            lead.next_followup_at = now + timedelta(days=3)
        elif lead.stage == "pitched":
            lead.stage = "followup_1"
            lead.next_followup_at = now + timedelta(days=4)
        else:
            lead.stage = "followup_done"
            lead.next_followup_at = None

        results.append({
            "clinic": lead.clinic_name,
            "new_stage": lead.stage,
            "email_sent": email_sent,
            "whatsapp_api_sent": wa_sent,
            "pitch_preview": pitch_msg[:120],
        })

    db.commit()

    summary_text = f"🤖 <b>AI Sales Agent Ran ({len(results)} Clinics)</b>\n"
    if wa_links_for_owner:
        summary_text += "\n📲 <b>1-Tap WhatsApp Links (No API Needed):</b>\n" + "\n".join(wa_links_for_owner[:5])
    await notify_owner(summary_text)

    return {"processed": len(results), "details": results}


@router.get("/ad-creative/{campaign_id}")
async def generate_patient_friendly_ad_creative(
    campaign_id: int, language: str = "hinglish", db: Session = Depends(get_db)
):
    camp = db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    clinic = db.query(ClinicDoctor).filter(ClinicDoctor.id == camp.clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    prompt = (
        f"Create patient-friendly Ad Copy & a 40-second Video Script in simple {language} (so even a village/common patient understands easily, ZERO tough medical English).\n"
        f"Clinic: {clinic.name}\nDegree: {clinic.degree}\nSpecialty: {clinic.specialty}\n"
        f"Plan: {camp.plan_type}\nCamp Title: {camp.camp_title or 'Regular Consultation'}\n"
        f"Address: {clinic.address}\n\n"
        f"Return JSON ONLY:\n"
        f'{{"poster_headline": "...", "simple_symptoms_covered": ["...", "..."], "poster_badge": "...", "audio_announcement": "2-line spoken message for patients", "video_script_40s": "..."}}'
    )
    raw = await generate_ai_text(prompt)
    try:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return {"campaign_id": campaign_id, "creative": json.loads(cleaned)}
    except Exception:
        return {"campaign_id": campaign_id, "creative_text": raw}


@router.get("/leads")
def list_crm_leads(stage: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(OutreachLead)
    if stage:
        q = q.filter(OutreachLead.stage == stage)
    leads = q.order_by(OutreachLead.id.desc()).limit(50).all()
    return {
        "total": len(leads),
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
    }
