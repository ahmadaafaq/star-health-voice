"""
tools/whatsapp.py — Send policy details to the customer via WhatsApp mid-call.

When a customer asks to receive information on WhatsApp, or when the agent
decides to share a policy document, this tool calls the star-health-web API
to send a WhatsApp message with the relevant policy PDF link.
"""

import logging
import os

import httpx
from livekit.agents import function_tool, RunContext
from dotenv import load_dotenv

load_dotenv(".env")
logger = logging.getLogger("star-health-agent.whatsapp")

WEB_API_URL = os.getenv("WEB_API_URL", "http://localhost:3000")
RAG_API_URL = os.getenv("RAG_API_URL", "http://localhost:8005")


@function_tool()
async def send_whatsapp_details(context: RunContext, policy_name: str = "") -> str:
    """
    Send a WhatsApp message to the customer with their recommended policy document.
    Call this when the customer asks to receive details on WhatsApp, or when you want
    to share a brochure/PDF to help them decide.

    Args:
        policy_name: Name of the policy to share (e.g., 'Young Star', 'Arogya Sanjeevani').
                     Leave empty to send the customer's recommended plan.
    """
    lead = context.userdata.get("lead", {})
    phone = lead.get("phone", "")
    name = lead.get("name", "Customer")
    recommended_plan = lead.get("recommended_plan") or lead.get("recommendedPlan", "")

    plan_to_send = policy_name.strip() or recommended_plan

    if not phone:
        logger.warning("No phone number found — cannot send WhatsApp")
        return "I wasn't able to send the WhatsApp message. Could you confirm your phone number?"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{RAG_API_URL}/send-welcome",
                json={
                    "phone": phone,
                    "name": name,
                    "policy_name": plan_to_send,
                },
            )
            response.raise_for_status()
        logger.info(f"WhatsApp sent to {phone} for plan {plan_to_send}")
        return "Done! I've sent the policy details to your WhatsApp number."
    except httpx.TimeoutException:
        logger.warning(f"WhatsApp send timeout for {phone}")
        return "It's taking a moment — the message should arrive on your WhatsApp shortly."
    except Exception as e:
        logger.error(f"Error sending WhatsApp: {e}")
        return "I've noted your request. Our team will send you the details on WhatsApp shortly."
