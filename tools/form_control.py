"""
tools/form_control.py — LiveKit data channel bridge for Voice Form Assistant mode.

Sends JSON commands to the customer's browser to update the insurance form in real time.
Message formats sent:
  {"type": "form_update", "field": "<name>", "value": <value>}
  {"type": "form_advance", "target_step": <N>}
  {"type": "form_submit"}
  {"type": "form_go_to_step", "step": <N>}

SERVER-SIDE STEP STATE:
  Tracks current step per room to prevent the LLM from spamming advance calls.
  - Only one advance is allowed per 5-second window per room.
  - Step is clamped to [1, 5]. Attempts to go past 5 are rejected.
"""

import json
import logging
import time
from typing import Dict
from livekit.agents import RunContext, function_tool

logger = logging.getLogger("star-health-agent.form_control")

# ── Per-room step state ──────────────────────────────────────────────────────
# Key: room name (str) → {"step": int, "last_advance_ts": float}
_room_step_state: Dict[str, dict] = {}

MIN_ADVANCE_COOLDOWN_SECS = 5.0  # Minimum seconds between advance calls
MAX_STEP = 5


def _get_state(ctx: RunContext) -> dict:
    """Get or create state for the current room."""
    room = ctx.userdata.get("room")
    room_name = room.name if room else "unknown"
    if room_name not in _room_step_state:
        # Prepopulate fields from existing lead object in userdata if available
        initial_fields = {}
        lead = ctx.userdata.get("lead")
        if lead and isinstance(lead, dict):
            initial_fields["lead_name"] = lead.get("name") or ""
            initial_fields["lead_phone"] = lead.get("phone") or ""
            initial_fields["lead_email"] = lead.get("email") or ""
            initial_fields["lead_gender"] = lead.get("gender") or ""
            initial_fields["age"] = lead.get("age") or ""
            
            members = lead.get("members") or []
            if "myself" in members:
                initial_fields["myself_selected"] = True
            if "spouse" in members:
                initial_fields["spouse_selected"] = True
            
            initial_fields["children_count"] = lead.get("children_count") or 0
            initial_fields["my_parents_count"] = lead.get("my_parents_count") or 0
            initial_fields["spouse_parents_count"] = lead.get("spouse_parents_count") or 0
            initial_fields["diabetes"] = lead.get("diabetes") or False
            initial_fields["pregnancy_plan"] = lead.get("pregnancy_plan") or False
            initial_fields["pre_existing"] = lead.get("pre_existing_diseases") or []
            initial_fields["city"] = lead.get("city") or ""
            initial_fields["budget"] = lead.get("budget") or ""
            initial_fields["employer_insurance"] = lead.get("employer_insurance") or False
            initial_fields["preferred_hospital"] = lead.get("preferred_hospital") or ""

        _room_step_state[room_name] = {
            "step": 1,
            "last_advance_ts": 0.0,
            "submitted": False,
            "last_update_ts": 0.0,
            "fields": initial_fields
        }
    return _room_step_state[room_name]


def set_room_step(room_name: str, step: int):
    """Set the current step for a room from external events (user navigation or initial context)."""
    if room_name not in _room_step_state:
        _room_step_state[room_name] = {
            "step": step,
            "last_advance_ts": 0.0,
            "submitted": False,
            "last_update_ts": 0.0,
            "fields": {}
        }
    else:
        _room_step_state[room_name]["step"] = step
        # Reset cooldown when step is manually set/navigated
        _room_step_state[room_name]["last_advance_ts"] = 0.0
    logger.info(f"Updated server-side step state for room {room_name} to step {step}")


async def _publish(ctx: RunContext, payload: dict) -> bool:
    """Publish a JSON payload to all participants via the LiveKit data channel."""
    room = ctx.userdata.get("room")
    if not room:
        logger.error("Room not found in userdata — cannot send form update.")
        return False
    try:
        await room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8"),
            reliable=True,
        )
        logger.info(f"Form data sent: {payload}")
        return True
    except Exception as e:
        logger.error(f"publish_data failed: {e}")
        return False


@function_tool
async def update_form_field(ctx: RunContext, field: str, value: str) -> str:
    """
    Update a field in the customer's insurance form in their browser instantly.
    Call this immediately after the customer gives each answer, before asking the next question.

    Args:
        field: Form field name. One of:
               myself_selected, spouse_selected, children_count, my_parents_count,
               spouse_parents_count, age, diabetes, pregnancy_plan, pre_existing,
               city, budget, employer_insurance, preferred_hospital,
               lead_name, lead_phone, lead_email, lead_gender
        value: The value as a string:
               Booleans → "true" or "false"
               Numbers  → "2" (children count etc.)
               city     → "tier-1", "tier-2", or "tier-3"
               budget   → "low", "moderate", or "premium"
               gender   → "male" or "female"
    """
    import re

    # ── VALIDATION: Enforce English characters for Name ──
    if field == "lead_name":
        if re.search(r"[\u0900-\u097f]", value):
            logger.warning(f"Name validation failed: Devanagari script detected in '{value}'")
            return f"ERROR: Always write lead_name using English characters only (Latin script). You provided '{value}' (in Devanagari). Please transliterate it to English letters and call update_form_field again."

    # ── VALIDATION: Enforce English characters for Email ──
    if field == "lead_email":
        if re.search(r"[\u0900-\u097f]", value):
            logger.warning(f"Email validation failed: Devanagari script detected in '{value}'")
            return f"ERROR: Always write lead_email using English characters only (Latin script). You provided '{value}' (in Devanagari). Please write it in English characters and call update_form_field again."

    # ── VALIDATION & CLEANING: Enforce 10-digit mobile phone number ──
    if field == "lead_phone":
        # Strip all non-digit characters
        cleaned = re.sub(r"\D", "", value)
        
        # Handle country code prefixes
        if len(cleaned) == 12 and cleaned.startswith("91"):
            cleaned = cleaned[2:]
        elif len(cleaned) == 11 and cleaned.startswith("0"):
            cleaned = cleaned[1:]
            
        if len(cleaned) != 10:
            logger.warning(f"Phone number validation failed: {value} cleaned to {cleaned} (len={len(cleaned)})")
            return f"ERROR: Phone number must be exactly 10 digits. The value you provided was cleaned to '{cleaned}' ({len(cleaned)} digits). Please ask the customer to repeat their 10-digit mobile number clearly."
        
        parsed_val: any = cleaned
    else:
        # Coerce string value to correct Python type for JSON serialization
        lv = value.lower().strip()
        if lv in ("true", "yes", "haan", "ha", "ji"):
            parsed_val = True
        elif lv in ("false", "no", "nahi", "nah", "nope"):
            parsed_val = False
        else:
            try:
                parsed_val = int(value)
            except (ValueError, TypeError):
                # Try JSON list (for pre_existing array)
                if lv.startswith("["):
                    try:
                        parsed_val = json.loads(value)
                    except Exception:
                        parsed_val = value
                else:
                    parsed_val = value

    state = _get_state(ctx)
    if "fields" not in state:
        state["fields"] = {}
    state["fields"][field] = parsed_val
    state["last_update_ts"] = time.monotonic()
    await _publish(ctx, {"type": "form_update", "field": field, "value": parsed_val})
    return "ok"


@function_tool
async def advance_form_step(ctx: RunContext) -> str:
    """
    Move the insurance form to the next step.
    Call this ONLY ONCE after the customer verbally confirms all fields for the current step.
    WARNING: This tool enforces a cooldown. Calling it multiple times will be rejected.
    You MUST wait for the customer to speak and say "yes" / "theek hai" / "confirm" before calling this.
    """
    state = _get_state(ctx)
    now = time.monotonic()

    # Guard 1: Already on the last step
    if state["step"] >= MAX_STEP:
        logger.warning(f"advance_form_step rejected: already on step {state['step']} (max={MAX_STEP})")
        return f"ERROR: Already on step {state['step']}, which is the last step. Cannot advance further. Use submit_form() instead if all details are collected."

    # Guard 2: Cooldown — prevent rapid-fire advances
    elapsed = now - state["last_advance_ts"]
    if elapsed < MIN_ADVANCE_COOLDOWN_SECS:
        logger.warning(f"advance_form_step rejected: cooldown active ({elapsed:.1f}s < {MIN_ADVANCE_COOLDOWN_SECS}s)")
        return "ERROR: Step was already advanced. Wait for the customer to confirm before advancing again."

    # All guards passed — advance
    state["step"] += 1
    state["last_advance_ts"] = now
    target_step = state["step"]

    logger.info(f"advance_form_step: advancing to step {target_step}")
    await _publish(ctx, {"type": "form_advance", "target_step": target_step})
    return f"ok — form moved to step {target_step}"


@function_tool
async def submit_form(ctx: RunContext) -> str:
    """
    Trigger final form submission to generate the AI insurance recommendation.
    Call this ONLY after the customer has verbally confirmed ALL details on Step 5
    (lead_name, lead_phone, lead_gender are filled and customer said yes/confirm).
    WARNING: This can only be called ONCE per session. Duplicate calls will be rejected.
    """
    state = _get_state(ctx)

    # Guard 1: Already submitted
    if state["submitted"]:
        logger.warning("submit_form rejected: form already submitted for this room")
        return "ERROR: Form has already been submitted. Do not call submit_form again."

    # Guard 2: Must be on step 5
    if state["step"] < MAX_STEP:
        logger.warning(f"submit_form rejected: currently on step {state['step']}, must be on step {MAX_STEP}")
        return f"ERROR: Cannot submit yet. You are on step {state['step']}. Complete all steps up to step {MAX_STEP} first."

    # Guard 3: Cannot submit in the same turn/batch as updating fields (must wait for user consent)
    now = time.monotonic()
    if now - state["last_update_ts"] < 2.0:
        logger.warning("submit_form rejected: called too close to update_form_field (same turn)")
        return "ERROR: Cannot call submit_form in the same turn as update_form_field. You must first verbally ask the customer to confirm their details and give consent. Only after they say yes/agree, you can call submit_form in the next turn."

    state["submitted"] = True
    logger.info("submit_form: submitting form")
    await _publish(ctx, {"type": "form_submit"})
    return "ok — form submitted successfully"


@function_tool
async def go_to_form_step(ctx: RunContext, step: int) -> str:
    """
    Go back or navigate to a specific step in the insurance form.
    Call this when the customer requests to edit, change, or go back to a previous page/details (e.g. "Mujhe details badalna hai", "age modify karni hai", etc.).

    Args:
        step: Step number to navigate to (integer between 1 and 5).
              Step 1: members/age
              Step 2: diabetes/pregnancy/pre_existing
              Step 3: city/budget
              Step 4: company insurance/preferred hospital
              Step 5: contact details (name/phone/email/gender)
    """
    # Clamp to valid range
    clamped = max(1, min(MAX_STEP, step))
    if clamped != step:
        logger.warning(f"go_to_form_step: clamped step {step} to {clamped}")

    # Update server state
    state = _get_state(ctx)
    state["step"] = clamped
    state["last_advance_ts"] = 0.0  # Reset cooldown when navigating back

    await _publish(ctx, {"type": "form_go_to_step", "step": clamped})
    return f"ok — navigated to step {clamped}"


@function_tool
async def get_current_form_state(ctx: RunContext) -> str:
    """
    Retrieve all the details currently filled in the customer's insurance form.
    Call this when the customer asks what details they have filled, wants to verify their profile,
    or asks for a summary of their answers.
    """
    state = _get_state(ctx)
    fields = state.get("fields", {})
    if not fields:
        return "No details have been filled in the form yet."
    
    summary_parts = []
    
    # 1. Members
    members = []
    if fields.get("myself_selected"):
        members.append("Self (आप)")
    if fields.get("spouse_selected"):
        members.append("Spouse (पति/पत्नी)")
    if fields.get("children_count"):
        members.append(f"{fields['children_count']} Children (बच्चे)")
    if fields.get("my_parents_count"):
        members.append(f"{fields['my_parents_count']} Parents (माता-पिता)")
    if fields.get("spouse_parents_count"):
        members.append(f"{fields['spouse_parents_count']} Spouse's Parents (सास-ससुर)")
    
    if members:
        summary_parts.append(f"Members to cover: {', '.join(members)}")
        
    # 2. Age
    if fields.get("age"):
        summary_parts.append(f"Age: {fields['age']} years")
        
    # 3. Medical
    medical = []
    if "diabetes" in fields:
        medical.append("Diabetes: Yes" if fields["diabetes"] else "Diabetes: No")
    if "pregnancy_plan" in fields:
        medical.append("Pregnancy Plan: Yes" if fields["pregnancy_plan"] else "Pregnancy Plan: No")
    if fields.get("pre_existing"):
        ped = fields['pre_existing']
        ped_str = ", ".join(ped) if isinstance(ped, list) else str(ped)
        if ped_str and ped_str != "[]":
            medical.append(f"Pre-existing conditions: {ped_str}")
    if medical:
        summary_parts.append(f"Medical history: {', '.join(medical)}")
        
    # 4. Location & Budget
    if fields.get("city"):
        summary_parts.append(f"City Tier: {fields['city']}")
    if fields.get("budget"):
        summary_parts.append(f"Budget category: {fields['budget']}")
        
    # 5. Employer & Hospital
    if "employer_insurance" in fields:
        summary_parts.append("Company Health Insurance: Yes" if fields["employer_insurance"] else "Company Health Insurance: No")
    if fields.get("preferred_hospital"):
        summary_parts.append(f"Preferred Hospital: {fields['preferred_hospital']}")
        
    # 6. Contact Details
    if fields.get("lead_name"):
        summary_parts.append(f"Name: {fields['lead_name']}")
    if fields.get("lead_phone"):
        summary_parts.append(f"Phone: {fields['lead_phone']}")
    if fields.get("lead_email"):
        summary_parts.append(f"Email: {fields['lead_email']}")
    if fields.get("lead_gender"):
        summary_parts.append(f"Gender: {fields['lead_gender']}")
        
    return "Here are the details currently filled in the form:\n" + "\n".join([f"- {s}" for s in summary_parts])
