"""
make_call.py — Dispatch an outbound SIP call via LiveKit Cloud + Vobiz trunk.

Usage:
    python make_call.py --to +919876543210
    python make_call.py --to +919876543210 --lead-id <uuid>

The script:
1. Creates a LiveKit room.
2. Creates a SIP participant (Vobiz trunk → customer phone).
3. Dispatches the star-health agent to that room.
The agent then handles the call using the lead context from Supabase.
"""

import os
import certifi

os.environ["SSL_CERT_FILE"] = certifi.where()

import argparse
import asyncio
import json
import logging
import time
import uuid

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("make-call")


async def dispatch_call(phone_number: str, lead_id: str = None):
    """
    Dispatch an outbound SIP call to `phone_number`.
    The call will be handled by the StarHealth agent running in LiveKit Cloud.
    """
    livekit_url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    sip_trunk_id = os.getenv("VOBIZ_SIP_TRUNK_ID")

    if not all([livekit_url, api_key, api_secret, sip_trunk_id]):
        logger.error("Missing LiveKit or Vobiz credentials in .env")
        return

    room_name = f"call-{lead_id or uuid.uuid4().hex[:8]}-{int(time.time())}"
    metadata = json.dumps({"lead_id": lead_id, "phone": phone_number}) if lead_id else "{}"

    logger.info(f"📞 Dispatching outbound call to {phone_number}")
    logger.info(f"   Room: {room_name}")
    logger.info(f"   Lead ID: {lead_id}")
    logger.info(f"   Trunk: {sip_trunk_id}")

    lk_api = api.LiveKitAPI(
        url=livekit_url,
        api_key=api_key,
        api_secret=api_secret,
    )

    try:
        # Create a room first so the agent can join it
        await lk_api.room.create_room(
            api.CreateRoomRequest(name=room_name, metadata=metadata)
        )
        logger.info(f"✅ Room created: {room_name}")

        # Create an outbound SIP participant (Vobiz → customer phone)
        sip_participant = await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=phone_number,       # E.164 format e.g. +919876543210
                room_name=room_name,
                participant_identity="phone-participant",
                participant_name=f"Call to {phone_number}",
                participant_metadata=metadata,
            )
        )
        logger.info(f"✅ SIP participant created: {sip_participant.participant_id}")
        logger.info(f"   Status: {sip_participant.sip_call_status}")

        # Dispatch the agent to handle the room
        await lk_api.agent.create_agent_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="star-health-agent",
                room=room_name,
                metadata=metadata,
            )
        )
        logger.info(f"✅ Agent dispatched to room {room_name}")
        logger.info("🎉 Call initiated! The agent will connect shortly.")

    except Exception as e:
        logger.error(f"❌ Failed to dispatch call: {e}", exc_info=True)
    finally:
        await lk_api.aclose()


def main():
    parser = argparse.ArgumentParser(description="Dispatch a Star Health outbound call via LiveKit.")
    parser.add_argument("--to", required=True, help="Customer phone number in E.164 format (e.g., +919876543210)")
    parser.add_argument("--lead-id", default=None, help="Supabase lead UUID (optional but recommended)")
    args = parser.parse_args()

    phone = args.to.strip()
    if not phone.startswith("+"):
        print("❌ Error: Phone number must start with '+' and country code (e.g., +919876543210)")
        return

    asyncio.run(dispatch_call(phone_number=phone, lead_id=args.lead_id))


if __name__ == "__main__":
    main()
