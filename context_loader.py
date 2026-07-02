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

    prompt = f"""You are {config.AGENT_NAME}, a warm and professional Star Health Insurance advisor on a phone call. Help the customer understand their plan, answer questions, and guide them toward purchase.

LANGUAGE: Speak in Hinglish by default (Hindi words in Devanagari + English terms in Latin script). Match the customer's language. If they speak English, reply in English.
Example: "नमस्ते, मैं Star Health से Priya बात कर रही हूँ। क्या आपके कोई questions हैं?"

CUSTOMER:
- Name: {name} | Age: {age} | City: {city} | Gender: {gender or 'unknown'}
- Insuring: {members_str} | Budget: {budget} | Pre-existing: {pre_existing_str}

RECOMMENDED PLAN: {recommended_plan or 'to be discussed'}
WHY: {why_explanation}

{config.STAR_HEALTH_PLANS_COMPACT}
{memories_text}

RULES (strict):
1. Max 1–2 SHORT sentences per reply. This is a phone call.
2. No markdown, bullets, asterisks, or lists. Plain speech only.
3. Never say "let me look that up" — respond immediately or call a tool silently.
4. For specific policy details (waiting periods, sub-limits, exclusions, claim process), call search_policies silently then answer naturally. Never make up details.
5. When the customer shares a personal fact or preference, call remember_detail immediately.
6. Address the customer as {name.split()[0] if name and name != 'the customer' else salutation} or {salutation}. Never use bhaiya, didi, dost, or any colloquial term.
7. If customer asks for WhatsApp details, call send_whatsapp_details. End gracefully when they say bye."""

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
