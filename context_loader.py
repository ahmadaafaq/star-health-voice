"""
context_loader.py — Pre-loads lead profile and conversation memories from Supabase
at the start of each call. All data is fetched ONCE and passed to the agent,
so there is zero per-turn retrieval latency for customer context.
"""

import logging
import os
from typing import Optional

from supabase import create_client, Client
from dotenv import load_dotenv

import config

load_dotenv(".env")
logger = logging.getLogger("star-health-agent")

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


async def preload_lead(lead_id: Optional[str]) -> tuple[dict, list]:
    """
    Fetch the lead profile and their past conversation memories from Supabase.
    Returns (lead_dict, memories_list).
    If no lead_id, returns ({}, []).
    """
    if not lead_id:
        logger.warning("No lead_id provided. Starting with empty context.")
        return {}, []

    db = _get_supabase()

    # Fetch lead profile
    lead = {}
    try:
        res = db.table("leads").select("*").eq("id", lead_id).maybe_single().execute()
        if res and res.data:
            lead = res.data
            logger.info(f"Loaded lead: {lead.get('name')} (id={lead_id})")
        else:
            logger.warning(f"No lead found for id={lead_id}")
    except Exception as e:
        logger.error(f"Error fetching lead {lead_id}: {e}")

    # Fetch past memories for this lead
    memories = []
    try:
        res = (
            db.table("agent_memories")
            .select("memory_type, content, updated_at")
            .eq("lead_id", lead_id)
            .order("updated_at", desc=True)
            .limit(15)
            .execute()
        )
        if res and res.data:
            memories = res.data
            logger.info(f"Loaded {len(memories)} memories for lead {lead_id}")
    except Exception as e:
        logger.warning(f"Could not fetch memories for lead {lead_id}: {e}. Continuing without memories.")

    return lead, memories


def build_system_prompt(lead: dict, memories: list) -> str:
    """
    Build the complete system prompt for the agent, injecting the lead profile
    and past memories so the LLM has full context before the first turn.
    """
    # ── Customer profile ────────────────────────────────────────────────────────
    name = lead.get("name", "the customer")
    age = lead.get("age", "unknown")
    city = lead.get("city", "unknown")
    members = lead.get("members") or []
    if isinstance(members, list):
        members_str = ", ".join(members) if members else "self"
    else:
        members_str = str(members)
    budget = lead.get("budget", "moderate")
    pre_existing = lead.get("pre_existing_conditions") or lead.get("preExisting") or []
    if isinstance(pre_existing, list):
        pre_existing_str = ", ".join(pre_existing) if pre_existing else "none"
    else:
        pre_existing_str = str(pre_existing)
    recommended_plan = lead.get("recommended_plan") or lead.get("recommendedPlan", "")
    phone = lead.get("phone", "")

    # ── Why this plan ───────────────────────────────────────────────────────────
    why_explanation = lead.get("recommendation_reason") or lead.get("why_this_plan") or _build_why_explanation(lead)

    # ── Past memories ───────────────────────────────────────────────────────────
    memories_text = ""
    if memories:
        memory_lines = [f"- {m['memory_type']}: {m['content']}" for m in memories]
        memories_text = "\nPAST CALL NOTES (what you already know about this customer):\n" + "\n".join(memory_lines)

    prompt = f"""You are {config.AGENT_NAME}, a warm, professional, and friendly Star Health Insurance advisor.
You are speaking to a customer on a phone call. Your goal is to help them understand their recommended plan, answer their questions, and guide them toward purchasing.

BILINGUAL HINDI & HINGLISH GUIDELINES:
- Speak in Hindi or Hinglish (Hindi mixed with common English words like 'policy', 'premium', 'hospital', 'room rent', 'claim') by default, matching how the customer speaks to you. If the customer speaks to you in English, you can reply in English.
- IMPORTANT SCRIPT FORMATTING FOR TTS SYNTHESIS:
  1. Write all Hindi words in Devanagari script (e.g., नमस्ते, कैसे हैं आप, मैं आपकी मदद कर सकती हूँ).
  2. Write all English words in Latin script (e.g., policy, claim, copay, waiting period, room rent, WhatsApp).
  - Example of Hinglish output: "नमस्ते, मैं Star Health से Priya बात कर रही हूँ। क्या मैं आपकी help कर सकती हूँ?"
  - This script-switching is critical for the voice engine to read the words in a natural, native accent.

CUSTOMER PROFILE:
- Name: {name}
- Age: {age}
- City: {city}
- Insuring: {members_str}
- Budget preference: {budget}
- Pre-existing conditions: {pre_existing_str}
- Phone: {phone}

RECOMMENDED PLAN: {recommended_plan or "to be discussed"}
WHY THIS PLAN: {why_explanation}

{config.STAR_HEALTH_PLANS_COMPACT}
{memories_text}

CRITICAL INSTRUCTIONS (follow these strictly):
1. Keep ALL responses to 1–2 SHORT sentences maximum. This is a phone call — brevity is essential.
2. Do NOT use markdown, bullet points, asterisks, or numbered lists. Speak in plain conversation.
3. Do NOT say "let me look that up" or "one moment please" — respond immediately.
4. When the customer asks about a specific policy detail you're not sure about, call the search_policies tool SILENTLY (do not announce it), then answer naturally.
5. When the customer tells you their name, preference, or any personal fact, call remember_detail immediately to save it.
6. If the customer asks to receive details on WhatsApp, call the send_whatsapp_details tool.
7. Always be warm, empathetic, and confident. Address the customer as {name.split()[0] if name and name != 'the customer' else "Sir/Ma'am"}.
8. If asked why this plan was recommended, explain it based on the WHY THIS PLAN section above.
9. Never make up policy details. Use search_policies if uncertain.
10. End the call gracefully when the customer says bye or goodbye."""

    return prompt


def _build_why_explanation(lead: dict) -> str:
    """Generate a simple why-explanation from the lead profile fields."""
    reasons = []
    members = lead.get("members") or lead.get("members", [])
    if isinstance(members, list):
        if "parents" in members or lead.get("parentsIncluded"):
            reasons.append("you included parents in the coverage")
        if "children" in members:
            reasons.append("you have children to protect")
        if "spouse" in members:
            reasons.append("you want to cover your spouse too")

    pre_existing = lead.get("pre_existing_conditions") or lead.get("preExisting") or []
    if isinstance(pre_existing, list) and pre_existing and "none" not in pre_existing:
        reasons.append(f"it covers pre-existing conditions like {', '.join(pre_existing)}")

    if lead.get("pregnancyPlan"):
        reasons.append("it includes maternity cover which you requested")

    age = lead.get("age", 0)
    try:
        age_int = int(age)
        if age_int >= 50:
            reasons.append("it is designed for senior citizens with no upper age limit")
    except (ValueError, TypeError):
        pass

    if not reasons:
        return "it matches your family size, budget, and coverage needs."

    return "because " + ", and ".join(reasons) + "."
