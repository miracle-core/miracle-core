import os
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Depends, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, get_db
from app import models  # noqa: F401
from app.telegram_bot import handle_telegram_update, notify_owner
from app.watchdog import run_watchdog, send_daily_report
from app.strategist_agent import run_strategist_analysis
from app.routers.jarvis_ai import router as jarvis_router
from app.routers.geo_ads import router as geo_ads_router
from app.routers.sales_agent import router as sales_router, run_ai_outreach_batch
from app.routers.admin_crm import router as admin_router

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "miracle_secret_2025")
CRON_SECRET = os.getenv("CRON_SECRET", "miracle_cron_2025")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="MIRACLE Health AI Core", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jarvis_router)
app.include_router(geo_ads_router)
app.include_router(sales_router)
app.include_router(admin_router)


@app.get("/")
def serve_app():
    return FileResponse("static/index.html")


@app.get("/admin")
def serve_admin():
    return FileResponse("static/admin.html")


@app.get("/track/{track_id}")
def serve_track(track_id: str):
    return FileResponse("static/track.html")


@app.get("/privacy")
def serve_privacy():
    return FileResponse("static/privacy.html")


@app.get("/logo.svg")
def serve_logo():
    return FileResponse("static/logo.svg", media_type="image/svg+xml")


@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "healthy", "database": "connected"}


@app.get("/api/cron/watchdog")
async def cron_watchdog(secret: str, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await run_watchdog(db)


@app.get("/api/cron/daily-report")
async def cron_daily_report(secret: str, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await send_daily_report(db)


@app.get("/api/cron/strategist")
async def cron_strategist(secret: str, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await run_strategist_analysis(db, notify_owner)


@app.get("/api/cron/outreach")
async def cron_outreach(secret: str, limit: int = 5, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return await run_ai_outreach_batch(limit=limit, db=db)


@app.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    return await handle_telegram_update(update, db)


@app.get("/api/telegram/setup-webhook")
async def setup_webhook(app_url: str, secret: str):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    webhook_url = f"{app_url.rstrip('/')}/api/telegram/webhook"
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={"url": webhook_url, "secret_token": WEBHOOK_SECRET},
        )
    await notify_owner("🚀 <b>MIRACLE v2.0 App, Voice Call & CRM Live!</b>")
    return res.json()
