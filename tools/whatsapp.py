"""
tools/whatsapp.py — Send policy details to the customer via WhatsApp mid-call.

When a customer asks to receive information on WhatsApp, this tool:
1. Checks if a phone number is on file.
2. If no phone → returns a gender-aware instruction for the agent to collect it.
3. If phone is provided (during call) → saves it to Supabase, updates userdata.
4. Sends the WhatsApp message via the RAG API.
"""

import logging
import os
import re

import httpx
from livekit.agents import function_tool, RunContext
from dotenv import load_dotenv

load_dotenv(".env")
logger = logging.getLogger("star-health-agent.whatsapp")

WEB_API_URL = os.getenv("WEB_API_URL", "http://localhost:3000")
RAG_API_URL = os.getenv("RAG_API_URL", "http://localhost:8005")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def _clean_phone(raw: str) -> str:
    """Strip non-digits, remove leading 91/0, return 10-digit string or ''."""
    cleaned = re.sub(r"\D", "", raw)
    if len(cleaned) == 12 and cleaned.startswith("91"):
        cleaned = cleaned[2:]
    elif len(cleaned) == 11 and cleaned.startswith("0"):
        cleaned = cleaned[1:]
    return cleaned if len(cleaned) == 10 else ""


async def _save_phone_to_db(lead_id: str, phone: str) -> bool:
    """Persist phone number to Supabase leads table."""
    if not lead_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/leads?id=eq.{lead_id}",
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={"phone": phone},
            )
            if resp.status_code in (200, 204):
                logger.info(f"Saved phone {phone} for lead {lead_id}")
                return True
            logger.warning(f"Supabase phone save failed: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error saving phone to DB: {e}")
    return False


@function_tool()
async def send_whatsapp_details(
    context: RunContext,
    policy_name: str = "",
    phone: str = "",
) -> str:
    """
    Send policy details to the customer's WhatsApp number.

    Call this when the customer asks to receive details on WhatsApp.
    IMPORTANT — before calling this, check the CUSTOMER CONTEXT in the system prompt:
      - If lead phone is available → call immediately with phone=""
      - If lead phone is missing → collect it from the customer first, then call again with phone="<number>"

    Args:
        policy_name: Name of the policy to share. Leave empty for recommended plan.
        phone: Only pass this if the customer just gave their number during THIS call
               (because it was not on file). Format: 10-digit mobile without country code.
    """
    lead = context.userdata.get("lead", {})
    lead_id = context.userdata.get("lead_id", "")
    name = lead.get("name", "Customer")
    gender = lead.get("gender") or lead.get("lead_gender") or ""
    recommended_plan = lead.get("recommended_plan") or lead.get("recommendedPlan", "")
    plan_to_send = policy_name.strip() or recommended_plan

    # ── Resolve phone ────────────────────────────────────────────────────────────
    # 1. Use caller-supplied phone if provided (customer gave it during call)
    # 2. Fallback to stored lead phone
    caller_phone = _clean_phone(phone) if phone else ""
    stored_phone = (lead.get("phone") or "").strip()
    resolved_phone = caller_phone or stored_phone

    # Gender-aware salutation for natural speech
    if gender == "male":
        salutation = "Sir"
    elif gender == "female":
        salutation = "Ma'am"
    else:
        salutation = "ji"

    # ── No phone available ───────────────────────────────────────────────────────
    if not resolved_phone:
        logger.warning("send_whatsapp_details: no phone — agent must collect it")
        return (
            f"NO_PHONE_ON_FILE: The customer has not shared their phone number. "
            f"Say exactly this in natural Hinglish: \"{salutation}, aapne humein apna phone number share nahi kiya tha. "
            f"WhatsApp par details bhejne ke liye please apna 10-digit mobile number share karein.\" "
            f"Then collect their number digit-by-digit, verify it, and call send_whatsapp_details again "
            f"with the phone parameter set to their number."
        )

    # ── Caller provided new phone — validate and save ────────────────────────────
    if caller_phone and not stored_phone:
        if len(caller_phone) != 10:
            return (
                f"INVALID_PHONE: The number '{phone}' has {len(caller_phone)} digits — need exactly 10. "
                f"Ask the customer to repeat their full 10-digit mobile number."
            )
        # Save to Supabase and update in-memory userdata
        saved = await _save_phone_to_db(lead_id, caller_phone)
        if saved:
            lead["phone"] = caller_phone  # reflect immediately in userdata
        logger.info(f"New phone {caller_phone} collected during call, saved={saved}")

    # ── Send WhatsApp ────────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{RAG_API_URL}/send-welcome",
                json={
                    "phone": resolved_phone,
                    "name": name,
                    "policy_name": plan_to_send,
                },
            )
            response.raise_for_status()
        logger.info(f"WhatsApp sent to {resolved_phone} for plan {plan_to_send}")
        return f"SENT: Policy details dispatched to WhatsApp {resolved_phone}. Tell the customer: 'Details aapke WhatsApp par bhej di hain — check kar lijiye.'"
    except httpx.TimeoutException:
        logger.warning(f"WhatsApp send timeout for {resolved_phone}")
        return (
            f"WHATSAPP_TIMEOUT: Service is slow. Say naturally: "
            f'"{salutation}, WhatsApp service mein abhi thoda issue hai — '
            f'aapko policy details call ke baad bhej di jaayengi. Koi baat nahi, agar koi aur sawaal ho to batayein."'
        )
    except Exception as e:
        logger.error(f"Error sending WhatsApp: {e}")
        return (
            f"WHATSAPP_ERROR: Service unavailable. Say naturally: "
            f'"{salutation}, WhatsApp service mein right now thoda technical issue dikh raha hai — '
            f"aapko details call ke baad send kar di jaayengi. Baaki sab theek hai, "
            f'koi aur cheez mein help kar sakti hoon?"'
        )
