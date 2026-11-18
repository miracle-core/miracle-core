import os
import io
import json
import time
import base64
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
import edge_tts
from gtts import gTTS

from app.database import get_db
from app.models import TriageLog
from app.prompts.medical_core import MIRACLE_SYSTEM_PROMPT
from app.strategist_agent import get_config, set_config, record_metric

router = APIRouter(prefix="/api/jarvis", tags=["Jarvis Medical AI"])

NEURAL_VOICES = {
    "hi": "hi-IN-SwaraNeural",
    "gu": "gu-IN-DhwaniNeural",
    "mr": "mr-IN-AarohiNeural",
    "bn": "bn-IN-TanishaaNeural",
    "ta": "ta-IN-PallaviNeural",
    "te": "te-IN-ShrutiNeural",
    "ur": "ur-PK-UzmaNeural",
    "en": "en-IN-NeerjaNeural",
    "tr": "tr-TR-EmelNeural",
    "ar": "ar-SA-ZariyahNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "id": "id-ID-GadisNeural",
    "pt": "pt-BR-FranciscaNeural",
}


class AskRequest(BaseModel):
    symptoms: str
    history: Optional[str] = ""
    language: Optional[str] = "auto-detect"
    lat: Optional[float] = None
    lng: Optional[float] = None


async def synthesize_clear_voice(text_to_speak: str, lang_code: str) -> str:
    if not text_to_speak or not text_to_speak.strip():
        return ""
    short_lang = (lang_code or "hi").split("-")[0].lower()
    voice_name = NEURAL_VOICES.get(short_lang, "en-IN-NeerjaNeural")

    # 1. Try Studio Neural Voice (edge-tts)
    try:
        comm = edge_tts.Communicate(text_to_speak[:900], voice_name)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        if len(buf) > 200:
            return base64.b64encode(bytes(buf)).decode("utf-8")
    except Exception:
        pass

    # 2. Reliable Backup (gTTS)
    try:
        tts_lang = short_lang if short_lang in NEURAL_VOICES else "en"
        mp3_fp = io.BytesIO()
        gTTS(text=text_to_speak[:900], lang=tts_lang, slow=False).write_to_fp(mp3_fp)
        mp3_fp.seek(0)
        return base64.b64encode(mp3_fp.read()).decode("utf-8")
    except Exception:
        return ""


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
    out 6;
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
        return out[:6]
    except Exception:
        return []


async def call_gemini_multimodal(
    db: Session,
    feature: str,
    user_prompt: str,
    media_bytes: bytes = None,
    mime_type: str = None,
) -> dict:
    start = time.time()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY Render Environment mein set nahi hai!")

    parts = [{"text": user_prompt}]
    if media_bytes and mime_type:
        clean_mime = mime_type.split(";")[0].strip()
        parts.append({"inlineData": {"mimeType": clean_mime, "data": base64.b64encode(media_bytes).decode("utf-8")}})

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
            "user_transcript": "",
            "detected_lang": "hi",
            "emergency_flag": False,
            "detected_department": "General Physician",
            "voice_reply": "Maaf kijiye, main dobara sunna chahunga. Kripya phir se bolein.",
            "simple_summary": str(data)[:500],
            "disclaimer": "Please consult a licensed doctor.",
        }

    record_metric(db, feature, 200, latency, parsed.get("detected_department"))
    return parsed


@router.post("/voice-turn")
async def voice_call_turn(
    audio: UploadFile = File(...),
    history: str = Form(default=""),
    preferred_lang: str = Form(default="auto"),
    lat: Optional[float] = Form(default=None),
    lng: Optional[float] = Form(default=None),
    db: Session = Depends(get_db),
):
    audio_bytes = await audio.read()
    if len(audio_bytes) < 500:
        raise HTTPException(status_code=400, detail="Audio bahut chota hai, kripya thoda saaf bolein.")

    nearby_verified = find_nearby_verified_care(db, "", lat, lng, radius_km=10)
    public_nearby = [] if nearby_verified else await fetch_public_nearby_names(lat, lng, radius_km=10)

    nearby_ctx = ""
    if nearby_verified:
        nearby_ctx = "Verified Clinics within 10km: " + ", ".join(
            [f"{c['name']} ({c['specialty']}, {c['distance_km']} km, Phone: {c['phone']})" for c in nearby_verified]
        )
    elif public_nearby:
        nearby_ctx = "Nearby Hospitals/Clinics within 10km: " + ", ".join(
            [f"{p['name']} ({p['type']})" for p in public_nearby]
        )

    prompt = (
        f"Listen carefully to the patient's attached voice audio.\n"
        f"1) Transcribe accurately into 'user_transcript'.\n"
        f"2) Auto-detect the exact language they spoke and reply in THAT EXACT SAME language (unless silent, then use '{preferred_lang}').\n"
        f"Conversation History:\n{history or 'None (Start of call)'}\n\n"
        f"Patient GPS Nearby Context:\n{nearby_ctx or 'No clinics found within 10km yet.'}\n\n"
        f"Answer warmly, specifically, and clearly in JSON."
    )

    ai_result = await call_gemini_multimodal(
        db, "jarvis_voice", prompt, media_bytes=audio_bytes, mime_type=audio.content_type or "audio/webm"
    )

    dept = ai_result.get("detected_department", "General Physician")
    detected_lang = ai_result.get("detected_lang", "hi")
    reply_text = (
        f"Emergency Alert! {ai_result.get('emergency_message', '')} {ai_result.get('voice_reply', '')}"
        if ai_result.get("emergency_flag")
        else ai_result.get("voice_reply") or ai_result.get("simple_summary") or ""
    )

    audio_b64 = await synthesize_clear_voice(reply_text, detected_lang)

    if ai_result.get("user_transcript"):
        log = TriageLog(
            language=detected_lang,
            detected_department=dept,
            detected_pathy="Voice-Call",
            emergency_flag=bool(ai_result.get("emergency_flag", False)),
            summary=(ai_result.get("simple_summary") or "")[:500],
        )
        db.add(log)
        db.commit()

    dept_verified = find_nearby_verified_care(db, dept, lat, lng, radius_km=10)
    return {
        "ai_guidance": ai_result,
        "audio_base64": audio_b64,
        "nearby_verified_specialists_10km": dept_verified or nearby_verified,
        "nearby_public_hospitals_10km": public_nearby,
    }


@router.post("/ask")
async def ask_jarvis(req: AskRequest, db: Session = Depends(get_db)):
    nearby_verified = find_nearby_verified_care(db, "", req.lat, req.lng, radius_km=10)
    public_nearby = [] if nearby_verified else await fetch_public_nearby_names(req.lat, req.lng, radius_km=10)

    nearby_ctx = ""
    if nearby_verified:
        nearby_ctx = "Verified Clinics within 10km: " + ", ".join(
            [f"{c['name']} ({c['specialty']}, {c['distance_km']} km)" for c in nearby_verified]
        )
    elif public_nearby:
        nearby_ctx = "Nearby Hospitals within 10km: " + ", ".join([f"{p['name']} ({p['type']})" for p in public_nearby])

    prompt = (
        f"Preferred/Hint Language: {req.language} (If user wrote in another language, switch to user's language!)\n"
        f"Conversation History: {req.history or 'None'}\n"
        f"Patient Message: {req.symptoms}\n"
        f"{nearby_ctx}\n"
        f"Answer specifically and clearly in JSON."
    )
    ai_result = await call_gemini_multimodal(db, "jarvis_ask", prompt)
    dept = ai_result.get("detected_department", "General Physician")
    detected_lang = ai_result.get("detected_lang", "hi")
    audio_b64 = await synthesize_clear_voice(ai_result.get("voice_reply") or ai_result.get("simple_summary"), detected_lang)

    log = TriageLog(
        language=detected_lang,
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
        "audio_base64": audio_b64,
        "nearby_verified_specialists_10km": dept_verified or nearby_verified,
        "nearby_public_hospitals_10km": public_nearby,
    }


@router.post("/scan-report")
async def scan_medical_report(
    file: UploadFile = File(...),
    note: str = Form(default=""),
    language: str = Form(default="auto"),
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
        f"Patient Note: {note or 'Explain this medical report or prescription simply and honestly.'}\n"
        f"Scan carefully, bust unnecessary fear, and return structured JSON."
    )
    ai_result = await call_gemini_multimodal(db, "scan_report", prompt, media_bytes=img_bytes, mime_type=file.content_type)
    dept = ai_result.get("detected_department", "General Physician")
    detected_lang = ai_result.get("detected_lang", "hi")
    audio_b64 = await synthesize_clear_voice(ai_result.get("voice_reply") or ai_result.get("simple_summary"), detected_lang)

    log = TriageLog(
        language=detected_lang,
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
        "audio_base64": audio_b64,
        "nearby_verified_specialists_10km": nearby,
    }
