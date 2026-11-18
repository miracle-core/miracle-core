import os
import json
import base64
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import TriageLog
from app.prompts.medical_core import MIRACLE_SYSTEM_PROMPT

router = APIRouter(prefix="/api/jarvis", tags=["Jarvis Medical AI"])

GEMINI_MODEL = "gemini-2.5-flash"


class AskRequest(BaseModel):
    symptoms: str
    history: Optional[str] = ""
    language: Optional[str] = "hinglish"  # hinglish | hi | en | tr
    lat: Optional[float] = None
    lng: Optional[float] = None


def find_nearby_verified_care(
    db: Session, department: str, lat: Optional[float], lng: Optional[float], radius_km: int = 10
):
    if lat is None or lng is None:
        return []
    try:
        query = text("""
            SELECT
                c.id, c.name, c.specialty, c.pathy, c.degree, c.phone, c.address,
                a.plan_type, a.camp_title,
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
              AND (LOWER(c.specialty) LIKE LOWER(:dept) OR LOWER(:dept) LIKE '%' || LOWER(c.specialty) || '%')
            ORDER BY distance_km ASC
            LIMIT 5;
        """)
        rows = db.execute(
            query,
            {
                "lat": lat,
                "lng": lng,
                "radius_meters": radius_km * 1000,
                "dept": f"%{department}%",
            },
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def call_gemini(user_prompt: str, image_bytes: bytes = None, mime_type: str = None) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured on server.")

    parts = [{"text": user_prompt}]
    if image_bytes and mime_type:
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })

    payload = {
        "systemInstruction": {"parts": [{"text": MIRACLE_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(url, json=payload)

    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"AI Provider Error: {res.text[:300]}")

    data = res.json()
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw_text)
    except Exception:
        return {
            "emergency_flag": False,
            "detected_department": "General Physician",
            "simple_summary": str(data)[:800],
            "disclaimer": "Please consult a licensed doctor for confirmation.",
        }


@router.post("/ask")
async def ask_jarvis(req: AskRequest, db: Session = Depends(get_db)):
    prompt = (
        f"Preferred Language: {req.language}\n"
        f"Patient Symptoms / Question: {req.symptoms}\n"
        f"Medical History / Ongoing Meds / Allergies: {req.history or 'Not provided'}\n"
        f"Analyze carefully and provide unbiased second-opinion guidance in JSON."
    )
    ai_result = await call_gemini(prompt)

    dept = ai_result.get("detected_department", "General Physician")
    emergency = bool(ai_result.get("emergency_flag", False))

    # Log anonymized triage
    log = TriageLog(
        language=req.language or "hinglish",
        detected_department=dept,
        detected_pathy="Multi-Pathy",
        emergency_flag=emergency,
        summary=(ai_result.get("simple_summary") or "")[:500],
    )
    db.add(log)
    db.commit()

    nearby = find_nearby_verified_care(db, dept, req.lat, req.lng, radius_km=10)

    return {
        "ai_guidance": ai_result,
        "nearby_verified_specialists_10km": nearby,
    }


@router.post("/scan-report")
async def scan_medical_report(
    file: UploadFile = File(...),
    note: str = Form(default=""),
    language: str = Form(default="hinglish"),
    lat: Optional[float] = Form(default=None),
    lng: Optional[float] = Form(default=None),
    db: Session = Depends(get_db),
):
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Please upload JPG, PNG, or WEBP photo of the report.")

    img_bytes = await file.read()
    if len(img_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large. Max 8 MB allowed.")

    prompt = (
        f"Preferred Language: {language}\n"
        f"Patient Note: {note or 'Please explain this medical report or prescription simply and honestly.'}\n"
        f"Scan this image carefully. Bust unnecessary fear, highlight abnormal values or medicine salts, "
        f"and return structured JSON."
    )
    ai_result = await call_gemini(prompt, image_bytes=img_bytes, mime_type=file.content_type)

    dept = ai_result.get("detected_department", "General Physician")
    log = TriageLog(
        language=language,
        detected_department=dept,
        detected_pathy="Report-Vision",
        emergency_flag=bool(ai_result.get("emergency_flag", False)),
        summary=(ai_result.get("simple_summary") or "")[:500],
    )
    db.add(log)
    db.commit()

    nearby = find_nearby_verified_care(db, dept, lat, lng, radius_km=10)

    return {
        "ai_guidance": ai_result,
        "nearby_verified_specialists_10km": nearby,
    }
