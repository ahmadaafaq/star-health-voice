"""
Star Health Voice Agent Server
──────────────────────────────
Replaces the old Node.js Express server on port 4000.
Exposes token generation and outbound triggers, and runs the cron scheduler.
"""

import os
import certifi

# Fix macOS SSL issues
os.environ["SSL_CERT_FILE"] = certifi.where()

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from supabase import create_client, Client
from livekit.api import AccessToken, VideoGrants

from make_call import dispatch_call

load_dotenv(".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("voice-agent-server")

app = FastAPI(title="Star Health Voice Agent Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_supabase: Optional[Client] = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        _supabase = create_client(url, key)
    return _supabase


# ─── Schemas ─────────────────────────────────────────────────────────────────

class TriggerOutboundRequest(BaseModel):
    leadId: str


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "star-health-voice-agent-server"}


@app.get("/api/voice/token")
async def get_token(leadId: Optional[str] = Query(None)):
    """
    Generate a secure LiveKit WebRTC AccessToken for browser voice client.
    """
    try:
        if leadId in ("undefined", "null", "anonymous", ""):
            leadId = None

        room_name = f"browser-room-{leadId or 'anonymous'}-{os.urandom(3).hex()}"
        participant_identity = f"customer-{leadId or os.urandom(3).hex()}"

        api_key = os.getenv("LIVEKIT_API_KEY")
        api_secret = os.getenv("LIVEKIT_API_SECRET")

        if not api_key or not api_secret:
            raise HTTPException(status_code=500, detail="LiveKit server credentials missing in .env")

        token = AccessToken(api_key, api_secret)
        token.with_grants(VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True
        ))
        token.identity = participant_identity
        token.name = f"Customer {leadId}" if leadId else "Web Visitor"
        token.metadata = json.dumps({"lead_id": leadId})

        jwt_token = token.to_jwt()
        logger.info(f"Generated LiveKit WebRTC token for lead {leadId or 'anonymous'} in room {room_name}")

        return {
            "token": jwt_token,
            "roomName": room_name,
            "livekitUrl": os.getenv("LIVEKIT_URL", "wss://insurance-agent-m3m6v0tz.livekit.cloud")
        }
    except Exception as e:
        logger.error(f"Error generating token: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/voice/form-token")
async def generate_form_token(mode: str = "", step: int = 1, planId: str = "", leadId: str = ""):
    """
    Generate a LiveKit WebRTC token for the Voice Form Assistant or Policy Advisor.
    No lead ID required — this is for anonymous pre-submission form sessions.
    Room name uses 'form-room-' prefix so agent.py detects form mode automatically.
    When mode=advisor, uses 'advisor-room-' prefix for policy advisor reconnection.
    """
    try:
        api_key = os.getenv("LIVEKIT_API_KEY")
        api_secret = os.getenv("LIVEKIT_API_SECRET")

        if not api_key or not api_secret:
            raise HTTPException(status_code=500, detail="LiveKit credentials missing in .env")

        is_advisor = mode == "advisor"
        if is_advisor and planId:
            room_name = f"advisor-room-{planId}-{os.urandom(4).hex()}"
        elif is_advisor:
            room_name = f"advisor-room-{os.urandom(4).hex()}"
        else:
            room_name = f"form-room-{os.urandom(4).hex()}"
        participant_identity = f"visitor-{os.urandom(3).hex()}"


        token = AccessToken(api_key, api_secret)
        token.with_grants(VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        ))
        token.identity = participant_identity
        token.name = "Web Visitor (Policy Advisor)" if is_advisor else "Web Visitor (Form Assistant)"
        meta_dict = {"mode": "advisor" if is_advisor else "form_filling"}
        if planId:
            meta_dict["plan_id"] = planId
        if leadId:
            meta_dict["lead_id"] = leadId
        token.metadata = json.dumps(meta_dict)

        jwt_token = token.to_jwt()
        logger.info(f"Generated {'advisor' if is_advisor else 'form-assistant'} token for room: {room_name}")

        return {
            "token": jwt_token,
            "roomName": room_name,
            "livekitUrl": os.getenv("LIVEKIT_URL", "wss://insurance-agent-m3m6v0tz.livekit.cloud"),
        }
    except Exception as e:
        logger.error(f"Error generating form token: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/voice/trigger-outbound")
async def trigger_outbound_call(req: TriggerOutboundRequest):
    """
    Manually trigger an outbound call for a lead ID from the dashboard.
    """
    lead_id = req.leadId
    logger.info(f"Manual outbound call request for lead ID: {lead_id}")

    try:
        db = _get_supabase()
        
        # 1. Fetch lead from Supabase
        res = db.table("leads").select("*").eq("id", lead_id).maybe_single().execute()
        if not res or not res.data:
            raise HTTPException(status_code=404, detail="Lead not found in Supabase")
        
        lead = res.data
        phone = lead.get("phone", "")
        if not phone:
            raise HTTPException(status_code=400, detail="Lead has no valid phone number")

        # 2. Mark call status as dialing
        db.table("leads").update({"call_status": "dialing"}).eq("id", lead_id).execute()

        # 3. Dispatch LiveKit SIP Call
        asyncio.create_task(dispatch_call(phone_number=phone, lead_id=lead_id))

        return {"success": True, "callSid": f"LK_DISPATCH_{os.urandom(4).hex().upper()}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger outbound call: {e}", exc_info=True)
        # Revert status on failure
        try:
            db = _get_supabase()
            db.table("leads").update({"call_status": "failed"}).eq("id", lead_id).execute()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))


# ─── Vobiz Webhook ────────────────────────────────────────────────────────────

def _normalize_phone_for_match(phone: str) -> str:
    """Strip country code / formatting so we can match against our 10-digit DB values."""
    if not phone:
        return ""
    cleaned = "".join(c for c in phone if c.isdigit())
    # If 12-digit with 91 prefix, strip to last 10
    if len(cleaned) == 12 and cleaned.startswith("91"):
        return cleaned[2:]
    # If 11-digit starting with 0, strip 0
    if len(cleaned) == 11 and cleaned.startswith("0"):
        return cleaned[1:]
    # Return last 10 digits as a best-effort
    return cleaned[-10:] if len(cleaned) >= 10 else cleaned


async def _save_vobiz_recording(payload: dict):
    """
    Background task: match the Vobiz call to a lead via phone number,
    then save recording URL, transcription, and duration to Supabase.
    """
    call_uuid      = payload.get("CallUUID", "")
    record_url     = payload.get("RecordUrl", "")       # audio file URL
    duration_str   = payload.get("RecordingDuration", "0")
    transcription  = payload.get("TranscriptionText", "") or payload.get("Transcription", "")
    from_number    = payload.get("From", "")
    to_number      = payload.get("To", "")

    if not record_url and not transcription:
        logger.info("Vobiz webhook: no RecordUrl or TranscriptionText — nothing to save.")
        return

    # Parse duration (Vobiz sends it as string, sometimes float)
    try:
        duration_secs = int(float(duration_str))
    except (ValueError, TypeError):
        duration_secs = None

    # Find lead — two-pass strategy:
    #   Pass 1: prefer leads with call_status='dialing' for that phone (the lead that was
    #           actually being called right now — most specific match).
    #   Pass 2: fall back to most recently created lead with that phone number.
    # This handles the case where multiple leads share the same phone number (e.g. the
    # agent tests an outbound call using their own number stored in different lead records).
    db = _get_supabase()
    lead_id = None
    for raw_phone in [from_number, to_number]:
        normalized = _normalize_phone_for_match(raw_phone)
        if not normalized:
            continue
        phone_filter = f"phone.eq.{normalized},phone.eq.+91{normalized},phone.eq.0{normalized}"

        # Pass 1 — lead that is currently being dialed (most specific)
        dialing_res = (
            db.table("leads")
            .select("id, phone, call_status")
            .or_(phone_filter)
            .eq("call_status", "dialing")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if dialing_res and dialing_res.data:
            lead_id = dialing_res.data[0]["id"]
            logger.info(f"Vobiz webhook: matched DIALING lead {lead_id} via phone {normalized}")
            break

        # Pass 2 — most recently created lead with that phone (fallback)
        recent_res = (
            db.table("leads")
            .select("id, phone")
            .or_(phone_filter)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if recent_res and recent_res.data:
            lead_id = recent_res.data[0]["id"]
            logger.info(f"Vobiz webhook: matched most-recent lead {lead_id} via phone {normalized}")
            break

    if not lead_id:
        logger.warning(
            f"Vobiz webhook: could not match lead for CallUUID={call_uuid} "
            f"(From={from_number}, To={to_number}). Recording not saved."
        )
        return

    # Build update payload — only set fields that arrived in this webhook
    update: dict = {"vobiz_call_uuid": call_uuid}
    if record_url:
        update["call_recording_url"] = record_url
    if transcription:
        update["call_transcription"] = transcription
    if duration_secs is not None:
        update["call_duration_seconds"] = duration_secs

    db.table("leads").update(update).eq("id", lead_id).execute()
    logger.info(
        f"Vobiz webhook: updated lead {lead_id} — "
        f"recording={'yes' if record_url else 'no'}, "
        f"transcription={'yes' if transcription else 'no'}, "
        f"duration={duration_secs}s"
    )


@app.post("/api/vobiz/webhook", response_class=PlainTextResponse)
async def vobiz_webhook(request: Request):
    """
    Vobiz recording / transcription webhook.

    Vobiz sends application/x-www-form-urlencoded with fields:
      CallUUID, RecordUrl, RecordingDuration, TranscriptionText, From, To, Direction ...

    We respond with 200 immediately (Vobiz requires < 1–2s response),
    then process in a background task.

    Webhook URL to configure in Vobiz:
      http://35-207-203-88.sslip.io/voice/api/vobiz/webhook
    """
    # Parse form body — Vobiz sends form-encoded, not JSON
    try:
        form = await request.form()
        payload = dict(form)
    except Exception:
        # Fallback: try JSON body
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    call_uuid = payload.get("CallUUID", "unknown")
    logger.info(f"Vobiz webhook received: CallUUID={call_uuid}, keys={list(payload.keys())}")

    # Fire-and-forget background task so we respond instantly
    asyncio.create_task(_save_vobiz_recording(payload))

    # Vobiz requires a 200 plain-text or XML response
    return "OK"



async def scheduled_calls_checker():
    """
    Background cron scheduler that queries Supabase every minute
    for pending or scheduled calls and dispatches them.
    """
    await asyncio.sleep(5)  # initial startup delay
    logger.info("Outbound calls background scheduler loop started.")
    
    while True:
        try:
            db = _get_supabase()
            now_iso = datetime.now(timezone.utc).isoformat()
            
            # Fetch pending or scheduled leads where scheduled_call_at <= NOW
            res = (
                db.table("leads")
                .select("*")
                .in_("call_status", ["pending", "scheduled"])
                .lte("scheduled_call_at", now_iso)
                .limit(10)
                .execute()
            )
            
            leads = res.data or []
            if leads:
                logger.info(f"Found {len(leads)} pending scheduled calls. Initiating...")
                
                for lead in leads:
                    lead_id = lead.get("id")
                    phone = lead.get("phone")
                    if not lead_id or not phone:
                        continue
                        
                    try:
                        logger.info(f"Triggering scheduled call: lead={lead_id}, phone={phone}")
                        # Mark as dialing
                        db.table("leads").update({"call_status": "dialing"}).eq("id", lead_id).execute()
                        # Dispatch LiveKit SIP call
                        await dispatch_call(phone_number=phone, lead_id=lead_id)
                    except Exception as err:
                        logger.error(f"Failed to initiate scheduled call for lead {lead_id}: {err}")
                        db.table("leads").update({"call_status": "failed"}).eq("id", lead_id).execute()
            
        except Exception as e:
            logger.error(f"Error in scheduler check loop: {e}")
            
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup_event():
    # Start the scheduled calls loop in the background
    asyncio.create_task(scheduled_calls_checker())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 4000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
