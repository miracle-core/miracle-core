import os, io, json, time, base64
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
from app.strategist_agent import record_metric

router = APIRouter(prefix="/api/jarvis", tags=["Jarvis Medical AI"])

NEURAL_VOICES = {
    "en": "en-US-GuyNeural", "hi": "hi-IN-MadhurNeural", "gu": "gu-IN-NiranjanNeural",
    "mr": "mr-IN-ManoharNeural", "bn": "bn-IN-BashkarNeural", "ta": "ta-IN-ValluvarNeural",
    "te": "te-IN-MohanNeural", "ur": "ur-PK-AsadNeural", "tr": "tr-TR-AhmetNeural",
    "ar": "ar-SA-HamedNeural", "es": "es-ES-AlvaroNeural", "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural", "ru": "ru-RU-DmitryNeural", "id": "id-ID-ArdiNeural",
    "pt": "pt-BR-AntonioNeural",
}

class AskRequest(BaseModel):
    symptoms: str
    history: Optional[str] = ""
    language: Optional[str] = "en"
    lat: Optional[float] = None
    lng: Optional[float] = None

async def synthesize_clear_voice(text_to_speak: str, lang_code: str) -> str:
    if not text_to_speak or not text_to_speak.strip():
        return ""
    short_lang = (lang_code or "en").split("-")[0].lower()
    voice_name = NEURAL_VOICES.get(short_lang, "en-US-GuyNeural")
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
    try:
        tts_lang = short_lang if short_lang in NEURAL_VOICES else "en"
        mp3_fp = io.BytesIO()
        gTTS(text=text_to_speak[:900], lang=tts_lang, slow=False).write_to_fp(mp3_fp)
        mp3_fp.seek(0)
        return base64.b64encode(mp3_fp.read()).decode("utf-8")
    except Exception:
        return ""

def find_nearby_verified_care(db: Session, department: str, lat: Optional[float], lng: Optional[float], radius_km: int = 10):
    if lat is None or lng is None:
        return []
    try:
        q = text("""
            SELECT c.id, c.name, c.specialty, c.pathy, c.degree, c.phone, c.address, a.plan_type, a.camp_title,
            ROUND((ST_Distance(ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) / 1000.0)::numeric, 2) AS distance_km
            FROM clinics_doctors c JOIN ad_campaigns a ON c.id = a.clinic_id
            WHERE a.status = 'active' AND a.end_date >= NOW() AND c.is_verified = TRUE
            AND ST_DWithin(ST_SetSRID(ST_MakePoint(c.lng, c.lat), 4326)::geography,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :radius_meters)
            AND (LOWER(c.specialty) LIKE LOWER(:dept) OR LOWER(:dept) LIKE '%' || LOWER(c.specialty) || '%')
            ORDER BY distance_km ASC LIMIT 5;
        """)
        return [dict(r) for r in db.execute(q, {"lat": lat, "lng": lng, "radius_meters": radius_km * 1000, "dept": f"%{department}%"}).mappings().all()]
    except Exception:
        return []

async def fetch_public_nearby_names(lat: Optional[float], lng: Optional[float], radius_km: int = 10):
    if lat is None or lng is None:
        return []
    q = f'[out:json][timeout:8];(node["amenity"~"hospital|clinic|doctors"](around:{radius_km * 1000},{lat},{lng}););out 6;'
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post("https://overpass-api.de/api/interpreter", data={"data": q})
        if res.status_code != 200:
            return []
        return [{"name": el["tags"]["name"], "type": el["tags"].get("amenity", "clinic")} for el in res.json().get("elements", []) if el.get("tags", {}).get("name")][:6]
    except Exception:
        return []

async def call_gemini_multimodal(db: Session, feature: str, user_prompt: str, media_bytes: bytes = None, mime_type: str = None) -> dict:
    start = time.time()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY missing in Render!")
    parts = [{"text": user_prompt}]
    if media_bytes and mime_type:
        parts.append({"inlineData": {"mimeType": mime_type.split(";")[0].strip(), "data": base64.b64encode(media_bytes).decode("utf-8")}})
    payload = {
        "systemInstruction": {"parts": [{"text": MIRACLE_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    res = None
    for model_name in ["gemini-3.8-flash", "gemini-2.5-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post(url, json=payload)
        if res.status_code == 200 or res.status_code != 404:
            break
    if res is not None and res.status_code == 404:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                m_res = await client.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}")
            if m_res.status_code == 200:
                fms = [m["name"].replace("models/", "") for m in m_res.json().get("models", []) if "flash" in m.get("name", "").lower() and "tts" not in m.get("name", "").lower() and "image" not in m.get("name", "").lower() and "generateContent" in m.get("supportedGenerationMethods", [])]
                for fm in reversed(fms):
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{fm}:generateContent?key={api_key}"
                    async with httpx.AsyncClient(timeout=45) as client:
                        res = await client.post(url, json=payload)
                    if res.status_code == 200:
                        break
        except Exception:
            pass
    latency = int((time.time() - start) * 1000)
    if res is None or res.status_code != 200:
        code = res.status_code if res is not None else 502
        record_metric(db, feature, code, latency)
        raise HTTPException(status_code=502, detail=f"Gemini API Error ({code}): {res.text[:250] if res else 'No response'}")
    data = res.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"].replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
    except Exception:
        parsed = {"user_transcript": "", "detected_lang": "en", "emergency_flag": False, "detected_department": "General Physician", "voice_reply": "Please repeat that clearly.", "simple_summary": str(data)[:400], "disclaimer": "Consult a licensed doctor."}
    record_metric(db, feature, 200, latency, parsed.get("detected_department"))
    return parsed

@router.get("/greet")
async def get_male_greeting(lang: str = "en"):
    l = lang.lower()
    if "tr" in l:
        txt, code = "Merhaba, ben Miracle'dan Ali Vepa AI. Size nasıl yardımcı olabilirim?", "tr"
    elif "gu" in l:
        txt, code = "નમસ્તે, હું મિરેકલથી અલી વેપા AI છું. આજે હું તમારી શું મદદ કરી શકું?", "gu"
    elif "ar" in l:
        txt, code = "مرحباً، أنا علي فيبا AI من ميراكل. كيف يمكنني مساعدتك اليوم؟", "ar"
    elif "hi" in l or "hinglish" in l or "mr" in l or "ur" in l:
        txt, code = "Namaste, main Miracle se Ali Vepa AI hoon. Batayein aaj main aapki kya madad kar sakta hoon?", "hi"
    else:
        txt, code = "Hello, I am Ali Vepa AI from Miracle. How can I help you today?", "en"
    return {"text": txt, "lang": code, "audio_base64": await synthesize_clear_voice(txt, code)}

@router.post("/voice-turn")
async def voice_call_turn(audio: UploadFile = File(...), history: str = Form(default=""), preferred_lang: str = Form(default="en"), lat: Optional[float] = Form(default=None), lng: Optional[float] = Form(default=None), db: Session = Depends(get_db)):
    audio_bytes = await audio.read()
    if len(audio_bytes) < 500:
        raise HTTPException(status_code=400, detail="Audio too short, please speak clearly.")
    nv = find_nearby_verified_care(db, "", lat, lng, 10)
    pn = [] if nv else await fetch_public_nearby_names(lat, lng, 10)
    ctx = ("Verified Clinics 10km: " + ", ".join([f"{c['name']} ({c['specialty']}, {c['distance_km']}km)" for c in nv])) if nv else ("Nearby Hospitals 10km: " + ", ".join([f"{p['name']} ({p['type']})" for p in pn]))
    prompt = f"Listen to patient audio. Transcribe into 'user_transcript'. Auto-detect exact spoken language and reply in THAT SAME language (fallback: {preferred_lang}).\nHistory:\n{history or 'Start'}\nContext:\n{ctx}\nReturn JSON."
    ai_res = await call_gemini_multimodal(db, "jarvis_voice", prompt, audio_bytes, audio.content_type or "audio/webm")
    dept, dlang = ai_res.get("detected_department", "General Physician"), ai_res.get("detected_lang", "en")
    rtxt = f"Emergency Alert! {ai_res.get('emergency_message','')} {ai_res.get('voice_reply','')}" if ai_res.get("emergency_flag") else (ai_res.get("voice_reply") or ai_res.get("simple_summary") or "")
    if ai_res.get("user_transcript"):
        db.add(TriageLog(language=dlang, detected_department=dept, detected_pathy="Voice-Call", emergency_flag=bool(ai_res.get("emergency_flag", False)), summary=(ai_res.get("simple_summary") or "")[:500]))
        db.commit()
    return {"ai_guidance": ai_res, "audio_base64": await synthesize_clear_voice(rtxt, dlang), "nearby_verified_specialists_10km": find_nearby_verified_care(db, dept, lat, lng, 10) or nv, "nearby_public_hospitals_10km": pn}

@router.post("/ask")
async def ask_jarvis(req: AskRequest, db: Session = Depends(get_db)):
    nv = find_nearby_verified_care(db, "", req.lat, req.lng, 10)
    pn = [] if nv else await fetch_public_nearby_names(req.lat, req.lng, 10)
    ctx = ("Verified Clinics 10km: " + ", ".join([f"{c['name']} ({c['specialty']}, {c['distance_km']}km)" for c in nv])) if nv else ("Nearby Hospitals 10km: " + ", ".join([f"{p['name']} ({p['type']})" for p in pn]))
    prompt = f"Language hint: {req.language} (switch if user wrote another language).\nHistory: {req.history or 'None'}\nMessage: {req.symptoms}\n{ctx}\nReturn JSON."
    ai_res = await call_gemini_multimodal(db, "jarvis_ask", prompt)
    dept, dlang = ai_res.get("detected_department", "General Physician"), ai_res.get("detected_lang", "en")
    db.add(TriageLog(language=dlang, detected_department=dept, detected_pathy="Multi-Pathy", emergency_flag=bool(ai_res.get("emergency_flag", False)), summary=(ai_res.get("simple_summary") or "")[:500]))
    db.commit()
    return {"ai_guidance": ai_res, "audio_base64": await synthesize_clear_voice(ai_res.get("voice_reply") or ai_res.get("simple_summary"), dlang), "nearby_verified_specialists_10km": find_nearby_verified_care(db, dept, req.lat, req.lng, 10) or nv, "nearby_public_hospitals_10km": pn}

@router.post("/scan-report")
async def scan_medical_report(file: UploadFile = File(...), note: str = Form(default=""), language: str = Form(default="en"), lat: Optional[float] = Form(default=None), lng: Optional[float] = Form(default=None), db: Session = Depends(get_db)):
    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/jpg"}:
        raise HTTPException(status_code=400, detail="Upload JPG, PNG, or WEBP.")
    img = await file.read()
    prompt = f"Language: {language}\nNote: {note or 'Explain this medical report calmly.'}\nReturn JSON."
    ai_res = await call_gemini_multimodal(db, "scan_report", prompt, img, file.content_type)
    dept, dlang = ai_res.get("detected_department", "General Physician"), ai_res.get("detected_lang", "en")
    db.add(TriageLog(language=dlang, detected_department=dept, detected_pathy="Report-Vision", emergency_flag=bool(ai_res.get("emergency_flag", False)), summary=(ai_res.get("simple_summary") or "")[:500]))
    db.commit()
    return {"ai_guidance": ai_res, "audio_base64": await synthesize_clear_voice(ai_res.get("voice_reply") or ai_res.get("simple_summary"), dlang), "nearby_verified_specialists_10km": find_nearby_verified_care(db, dept, lat, lng, 10)}
