from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
)
from app.database import Base


class ClinicDoctor(Base):
    __tablename__ = "clinics_doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    specialty = Column(String(120), nullable=False)  # e.g. Cardiology, Ayurveda, Dental
    pathy = Column(String(80), default="Allopathy")  # Allopathy, Ayurveda, Homeopathy, etc.
    degree = Column(String(120), nullable=True)      # MBBS, BAMS, MD, BPT, etc.
    license_no = Column(String(100), nullable=True)
    phone = Column(String(40), nullable=True)
    email = Column(String(150), nullable=True)
    address = Column(Text, nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics_doctors.id"), nullable=False)
    plan_type = Column(String(50), nullable=False)  # camp_1500 | weekly_3500 | monthly_7500
    amount = Column(Integer, nullable=False)
    radius_km = Column(Integer, default=10)
    camp_title = Column(String(200), nullable=True)
    status = Column(String(30), default="pending")  # pending | active | expired | stopped
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("ad_campaigns.id"), nullable=False)
    order_code = Column(String(50), unique=True, index=True)
    amount = Column(Integer, nullable=False)
    ai_extracted_ref = Column(String(150), nullable=True)
    screenshot_url = Column(Text, nullable=True)
    status = Column(String(30), default="pending")  # pending | approved | rejected
    created_at = Column(DateTime, default=datetime.utcnow)


class OutreachLead(Base):
    __tablename__ = "outreach_leads"

    id = Column(Integer, primary_key=True, index=True)
    clinic_name = Column(String(200), nullable=False)
    specialty = Column(String(120), nullable=True)
    phone = Column(String(40), nullable=True)
    email = Column(String(150), nullable=True)
    city = Column(String(100), nullable=True)
    stage = Column(String(40), default="new")  # new | pitched | followup_1 | closed
    next_followup_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TriageLog(Base):
    __tablename__ = "triage_logs"

    id = Column(Integer, primary_key=True, index=True)
    language = Column(String(30), default="hi")
    detected_department = Column(String(100), nullable=True)
    detected_pathy = Column(String(80), nullable=True)
    emergency_flag = Column(Boolean, default=False)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CampBooking(Base):
    __tablename__ = "camp_bookings"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("ad_campaigns.id"), nullable=False)
    patient_name = Column(String(120), nullable=False)
    patient_phone = Column(String(40), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(80), primary_key=True, index=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class UsageMetric(Base):
    __tablename__ = "usage_metrics"

    id = Column(Integer, primary_key=True, index=True)
    feature = Column(String(80), nullable=False)  # jarvis_ask | scan_report | nearby_ads | register_ad
    department = Column(String(100), nullable=True)
    status_code = Column(Integer, default=200)
    latency_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SosTrack(Base):
    __tablename__ = "sos_tracks"

    track_id = Column(String(30), primary_key=True, index=True)
    patient_name = Column(String(120), default="Patient")
    blood_group = Column(String(20), default="")
    medical_note = Column(String(250), default="")
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
