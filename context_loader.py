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
    first_name_raw = name.split()[0] if name and name != "the customer" else ""
    first_name = config.COMMON_NAMES_MAP.get(first_name_raw, first_name_raw)
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
    recommended_plan = (
        lead.get("recommended_plan_id")
        or lead.get("recommended_plan")
        or lead.get("recommendedPlan")
        or ""
    )
    plan_hi = config.PLAN_NAME_MAP.get(recommended_plan, recommended_plan)
    phone = lead.get("phone", "")

    # ── Why this plan ───────────────────────────────────────────────────────────
    why_explanation = lead.get("recommendation_reason") or lead.get("why_this_plan") or _build_why_explanation(lead)

    # ── Past memories ───────────────────────────────────────────────────────────
    memories_text = ""
    if memories:
        memory_lines = [f"- {m['memory_type']}: {m['content']}" for m in memories]
        memories_text = "\nPAST CALL NOTES (what you already know about this customer):\n" + "\n".join(memory_lines)

    # Build plan name mapping table for LLM prompt context
    plan_map_text = "\n".join([f"- {eng} -> {hi}" for eng, hi in config.PLAN_NAME_MAP.items()])

    prompt = f"""You are {config.AGENT_NAME}, a warm and professional Star Health Insurance digital advisor. Speak in Hinglish.

HINGLISH LANGUAGE RULES:
1. MIXTURE & BALANCE: Maintain a natural, casual 50-50 balance of Hindi and English in every sentence. Do NOT speak only pure English or only pure Hindi. Flow naturally.
2. NO TOUGH HINDI: Never use tough, formal, or textbook Hindi words. Speak the way normal urban people chat. For example:
   - Use English words like: features, benefits, details, check, confirm, process, update, budget, standard, dynamic, whatsapp, message.
   - Replace tough Hindi words with simple English equivalents:
     * Use "check करना" (NEVER "पुष्टि/समीक्षा करना")
     * Use "details send करना" (NEVER "विवरण भेजना")
     * Use "features / benefits" (NEVER "विशेषताएं / लाभ")
     * Use "process / rules" (NEVER "प्रक्रिया / नियम")
     * Use "confirm / decide करना" (NEVER "अनुरोध / निर्णय करना")
3. SCRIPT STYLE: Write Hindi words in Devanagari script (e.g. नमस्ते, क्या आप, बात कर रही हूँ) and English words in English script (e.g. details, confirm, features, plan).
4. PLAN NAMES: ALWAYS write plan names in Devanagari phonetics so TTS pronounces them correctly (e.g. "यंग स्टार", "फैमिली हेल्थ ऑप्टिमा", "स्टार हेल्थ एश्योर"). Never write plan names in English letters.
5. MONEY PRONUNCIATION: Write out all currency amounts and sum insured limits in full words (e.g. write "5 Lakh Rupees" instead of "₹5L" or "Rs. 5 Lakh", and write "799 Rupees per month" instead of "₹799/month"). Never output "₹", "Rs", "L", "Cr", "mo" in your text response.

CUSTOMER PROFILE:
- Name: {name} (First name: {first_name})
- Age: {age} | City: {city} | Gender: {gender or 'unknown'}
- Insuring: {members_str} | Budget: {budget} | Pre-existing: {pre_existing_str}
- Recommended Plan: {plan_hi}
- Why: {why_explanation}

{config.STAR_HEALTH_PLANS_COMPACT}
{memories_text}

CONVERSATIONAL RULES:
1. Speak in exactly 1-2 short sentences. Answer directly — no filler phrases, no echoing the question.
2. For policy details (waiting periods, exclusions, limits, sub-limits), call 'search_policies' immediately with a single combined query. Never chain multiple search calls in one turn.
3. FAMILY UPGRADE: If customer asks about adding family members to an individual plan — proactively suggest a floater plan instead (e.g. "यंग स्टार individual plan है, family के लिए फैमिली हेल्थ ऑप्टिमा या स्टार हेल्थ एश्योर better option है।").
4. For WhatsApp requests, trigger send_whatsapp_details immediately.
5. When customer says bye, end the call gracefully.
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
