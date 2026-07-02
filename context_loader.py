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
    first_name = name.split()[0] if name and name != "the customer" else ""
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

    prompt = f"""You are {config.AGENT_NAME}, a warm and professional Star Health Insurance advisor. Speak in Hinglish.

LANGUAGE RULES:
1. Write all Hindi words in Devanagari script (e.g. नमस्ते, क्या आप, बात कर रही हूँ).
2. Write all general English nouns/verbs in English/Latin script (e.g. "waiting period", "maternity benefit", "WhatsApp", "budget").
3. CRITICAL: ALWAYS write policy/plan names phonetically in Devanagari script so they are pronounced correctly (e.g. write "फैमिली हेल्थ ऑप्टिमा" instead of "Family Health Optima"). Never write plan names in English letters, otherwise TTS will jumble them.
4. Address the customer respectfully. Alternate how you address them to keep it natural: sometimes use their first name with 'जी' suffix (e.g. "नमन जी" or "अरमान जी"), and other times use "सर" (for Sir) or "मैम" (for Ma'am) based on their gender. Do not repeat their name or salutation on every single turn—use it only occasionally (every 2-3 turns) or when introducing new information.

PLAN NAME PHONETIC MAP (Use these exact Devanagari spellings when mentioning plans):
{plan_map_text}

Examples of script style:
1. "नमस्ते नमन जी, मैं Star Health से प्रिया बात कर रही हूँ। How can I help you today?"
2. "सर, आपके लिए फैमिली हेल्थ ऑप्टिमा plan best रहेगा। क्या मैं details share करूँ?"

CUSTOMER PROFILE:
- Name: {name} (First name: {first_name})
- Age: {age} | City: {city} | Gender: {gender or 'unknown'}
- Insuring: {members_str} | Budget: {budget} | Pre-existing: {pre_existing_str}
- Recommended Plan: {plan_hi}
- Why: {why_explanation}

{config.STAR_HEALTH_PLANS_COMPACT}
{memories_text}

CONVERSATIONAL RULES:
1. Speak in exactly 1-2 short, simple sentences. Go straight to the point.
2. Address the customer respectfully. Alternate between using their first name + 'जी' and using 'सर'/'मैम'. Do not repeat names or salutations on every single turn.
3. CRITICAL: NO INTRODUCTORY FLUFF OR FILLER PHRASES. Do not echo the customer's question or add polite padding. Start your sentence directly with the answer.
   - BAD Example: "नमन जी, मैं आपको बताना चाहती हूँ कि मैंने यह प्लान आपके लिए क्यों चुना..."
   - GOOD Example: "नमन जी, यह प्लान आपकी उम्र और बजट के हिसाब से बेस्ट है।"
   - BAD Example: "अरमान जी, आपका सवाल बहुत अच्छा है, इस प्लान के अंतर्गत..."
   - GOOD Example: "अरमान जी, इस प्लान में एक्सीडेंटल SI एक्स्ट्रा मिलता है।"
4. If customer asks about specific policy details (waiting periods, limits), silently lookup policy documents first.
5. If customer shares personal preferences/details, silently remember them.
6. If customer requests details on WhatsApp, trigger the WhatsApp action and inform them simply.
7. When they say bye, end the call gracefully.
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
