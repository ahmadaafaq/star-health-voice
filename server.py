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
from fastapi import FastAPI, HTTPException, Query
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


# ─── Cron Scheduler ─────────────────────────────────────────────────────────

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
