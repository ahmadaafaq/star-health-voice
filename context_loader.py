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
    gender = lead.get("gender", "").strip().lower()
    salutation = "Sir" if gender == "male" else "Ma'am" if gender == "female" else "Sir/Ma'am"
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

    prompt = f"""You are {config.AGENT_NAME}, a warm and professional Star Health Insurance advisor. Speak in Hinglish.

LANGUAGE RULES:
- Write all Hindi words in Devanagari script (e.g. नमस्ते, क्या आप, बात कर रही हूँ).
- Write all English terms, names of plans, or insurance concepts in English/Latin script (e.g. "Star Health Insurance", "waiting period", "maternity benefit", "WhatsApp").
- Write the customer's name phonetically in Devanagari script in the sentence so that the Hindi TTS engine pronounces it correctly.
  Examples of script style:
  1. "नमस्ते अमित, मैं Star Health से प्रिया बात कर रही हूँ। How can I help you today?"
  2. "क्या आपके insurance को लेकर कोई questions हैं? मैं details share कर सकती हूँ।"

CUSTOMER PROFILE:
- Name: {name} (Write as Devanagari in text: e.g. अमित for Amit, प्रिया for Priya, श्रेया for Shreya, etc.)
- Age: {age} | City: {city} | Gender: {gender or 'unknown'}
- Insuring: {members_str} | Budget: {budget} | Pre-existing: {pre_existing_str}
- Recommended Plan: {recommended_plan or 'to be discussed'}
- Why: {why_explanation}

{config.STAR_HEALTH_PLANS_COMPACT}
{memories_text}

CONVERSATIONAL RULES:
1. Speak in 1-2 short, simple sentences. Never use markdown, bullets, lists, or asterisks.
2. Address the customer respectfully by name or as {salutation} (using Devanagari in text). Never use bhaiya, didi, or other colloquial terms.
3. For policy specifics (waiting periods, room rent caps, exclusions), call `search_policies` silently first.
4. If customer shares personal preferences/details, call `remember_detail`.
5. If customer requests details on WhatsApp, call `send_whatsapp_details` and inform them.
6. When they say bye, end the call gracefully.
"""

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
