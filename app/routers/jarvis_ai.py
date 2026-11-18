import os
import json
import time
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
from app.strategist_agent import get_config, set_config, record_metric

router = APIRouter(prefix="/api/jarvis", tags=["Jarvis Medical AI"])


class AskRequest(BaseModel):
    symptoms: str
    history: Optional[str] = ""
    language: Optional[str] = "hinglish"
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
            {"lat": lat, "lng": lng, "radius_meters": radius_km * 1000, "dept": f"%{department}%"},
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def fetch_public_nearby_names(lat: Optional[float], lng: Optional[float], radius_km: int = 10):
    if lat is None or lng is None:
        return []
    overpass_query = f"""
    [out:json][timeout:8];
    (
      node["amenity"~"hospital|clinic|doctors"](around:{radius_km * 1000},{lat},{lng});
    );
    out 5;
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query})
        if res.status_code != 200:
            return []
        out = []
        for el in res.json().get("elements", []):
            name = el.get("tags", {}).get("name")
            if name:
                out.append({"name": name, "type": el.get("tags", {}).get("amenity", "clinic")})
        return out[:5]
    except Exception:
        return []


async def call_openai(user_prompt: str, image_bytes: bytes = None, mime_type: str = None) -> dict:
    oai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not oai_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing.")
    content = [{"type": "text", "text": user_prompt}]
    if image_bytes and mime_type:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
    payload = {
        "model": "gpt-4o-mini",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": MIRACLE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=45) as client:
        res = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {oai_key}"},
            json=payload,
        )
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"OpenAI Error: {res.text[:250]}")
    return json.loads(res.json()["choices"][0]["message"]["content"])


async def call_ai_smart(
    db: Session, feature: str, user_prompt: str, image_bytes: bytes = None, mime_type: str = None
) -> dict:
    start = time.time()
    provider = get_config(db, "ai_provider")

    if provider == "openai" and os.getenv("OPENAI_API_KEY"):
        res_json = await call_openai(user_prompt, image_bytes, mime_type)
        record_metric(db, feature, 200, int((time.time() - start) * 1000), res_json.get("detected_department"))
        return res_json

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY Render Environment mein set nahi hai!")

    parts = [{"text": user_prompt}]
    if image_bytes and mime_type:
        parts.append({"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("utf-8")}})

    payload = {
        "systemInstruction": {"parts": [{"text": MIRACLE_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }

    res = None
    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post(url, json=payload)
        if res.status_code == 200:
            break
        if res.status_code not in (400, 404):
            break

    latency = int((time.time() - start) * 1000)

    if res.status_code == 429 and os.getenv("OPENAI_API_KEY"):
        record_metric(db, feature, 429, latency)
        set_config(db, "ai_provider", "openai")
        return await call_openai(user_prompt, image_bytes, mime_type)

    if res.status_code != 200:
        record_metric(db, feature, res.status_code, latency)
        raise HTTPException(status_code=502, detail=f"Gemini API Error ({res.status_code}): {res.text[:250]}")

    data = res.json()
    try:
        raw_txt = data["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = raw_txt.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
    except Exception:
        parsed = {
            "emergency_flag": False,
            "detected_department": "General Physician",
            "voice_reply": "Aapki baat samajh aa gayi. Kripya apni takleef thodi aur detail mein batayein.",
            "simple_summary": str(data)[:600],
            "disclaimer": "Please consult a licensed doctor for confirmation.",
        }

    record_metric(db, feature, 200, latency, parsed.get("detected_department"))
    return parsed


@router.post("/ask")
async def ask_jarvis(req: AskRequest, db: Session = Depends(get_db)):
    # Pre-fetch nearby clinics & public hospitals so Jarvis can mention them directly in voice!
    nearby_verified = find_nearby_verified_care(db, "", req.lat, req.lng, radius_km=10)
    public_nearby = [] if nearby_verified else await fetch_public_nearby_names(req.lat, req.lng, radius_km=10)

    nearby_context = ""
    if nearby_verified:
        nearby_context = "Verified Clinics within 10km: " + ", ".join(
            [f"{c['name']} ({c['specialty']}, {c['distance_km']} km)" for c in nearby_verified]
        )
    elif public_nearby:
        nearby_context = "Nearby Public Hospitals/Clinics within 10km: " + ", ".join(
            [f"{p['name']} ({p['type']})" for p in public_nearby]
        )

    prompt = (
        f"Preferred Language: {req.language}\n"
        f"Conversation History: {req.history or 'None (First message)'}\n"
        f"Patient Latest Message / Question: {req.symptoms}\n"
        f"Patient GPS Location Available: {'Yes' if (req.lat and req.lng) else 'No'}\n"
        f"{nearby_context}\n\n"
        f"Answer directly and warmly in JSON. If they ask which doctor or where nearby, use the specialist & nearby hospitals context."
    )
    ai_result = await call_ai_smart(db, "jarvis_ask", prompt)
    dept = ai_result.get("detected_department", "General Physician")

    log = TriageLog(
        language=req.language or "hinglish",
        detected_department=dept,
        detected_pathy="Multi-Pathy",
        emergency_flag=bool(ai_result.get("emergency_flag", False)),
        summary=(ai_result.get("simple_summary") or "")[:500],
    )
    db.add(log)
    db.commit()

    dept_verified = find_nearby_verified_care(db, dept, req.lat, req.lng, radius_km=10)
    return {
        "ai_guidance": ai_result,
        "nearby_verified_specialists_10km": dept_verified or nearby_verified,
        "nearby_public_hospitals_10km": public_nearby,
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
        raise HTTPException(status_code=400, detail="Please upload JPG, PNG, or WEBP photo.")

    img_bytes = await file.read()
    if len(img_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large. Max 8 MB allowed.")

    prompt = (
        f"Preferred Language: {language}\n"
        f"Patient Note: {note or 'Please explain this medical report or prescription simply and honestly.'}\n"
        f"Scan carefully, bust unnecessary fear, and return structured JSON."
    )
    ai_result = await call_ai_smart(db, "scan_report", prompt, image_bytes=img_bytes, mime_type=file.content_type)
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
    return {"ai_guidance": ai_result, "nearby_verified_specialists_10km": nearby}
