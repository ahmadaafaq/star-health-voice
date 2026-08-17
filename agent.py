"""
Star Health Insurance Voice Agent
──────────────────────────────────
Stack: LiveKit Cloud · Deepgram STT (Nova-2) · Groq LLM (llama-3.1-8b-instant) · Sarvam TTS (Anushka)
Memory: Supabase agent_memories table (persistent across calls)
RAG: In-process FAISS search (all-MiniLM-L6-v2, prewarmed) — HTTP fallback via star-health-rag

Usage:
    python agent.py dev       # local console test (no SIP)
    python agent.py start     # connect to LiveKit Cloud, wait for calls
"""

import os
import re
import certifi
import io
import time

# Fix macOS SSL issues — must be before all other imports
os.environ["SSL_CERT_FILE"] = certifi.where()

import asyncio
import json
import logging
import httpx
from datetime import datetime, timezone
from typing import Any, AsyncIterable

from dotenv import load_dotenv
from livekit import agents
from livekit.api import LiveKitAPI
from livekit.protocol.egress import (
    RoomCompositeEgressRequest,
    EncodedFileOutput,
    EncodedFileType,
    StopEgressRequest,
)
try:
    from livekit.protocol.egress import ListEgressRequest
except ImportError:
    ListEgressRequest = None
from livekit.agents import (
    Agent,
    AgentSession,
    ChatContext,
    JobContext,
    JobProcess,
    RunContext,
    WorkerOptions,
    cli,
)
from livekit.rtc import ConnectionState
from livekit.plugins import deepgram, openai, silero, sarvam
try:
    from livekit.plugins import elevenlabs
except ImportError:
    elevenlabs = None

import config
from context_loader import preload_lead, build_system_prompt, build_form_assistant_prompt
from tools.memory import remember_detail, recall_detail, search_memories
from tools.phrase_tokenizer import PhraseTokenizer
from tools.policy_rag import search_policies
from tools.whatsapp import send_whatsapp_details
from tools.form_control import (
    update_form_field,
    advance_form_step,
    submit_form,
    go_to_form_step,
    set_room_step,
    get_current_form_state,
    _get_state,
)

load_dotenv(".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("star-health-agent")


# ─── Browser Call Recording Helper ───────────────────────────────────────────

async def _save_browser_call(
    ctx: JobContext,
    lead_id: str | None,
    room_name: str,
    egress_id: str | None,
    recording_path: str | None,
    conversation_log: list,
    call_start_ts: float,
) -> str | None:
    """
    Called after a browser (WebRTC) call room disconnects.
    1. Resolve or create lead_id if missing.
    2. Stop the LiveKit Egress (or download+upload if S3 creds missing).
    3. Build a readable transcript from the conversation log.
    4. Save recording path + transcript + duration to browser_call_* columns on the lead.
    Returns the resolved lead_id.
    """
    from context_loader import preload_lead
    db = preload_lead.__globals__["_get_supabase"]()

    # ── Step 0: Resolve or create lead_id if missing ─────────────────────────
    if not lead_id:
        try:
            # 1. Try checking ctx proc userdata
            if ctx and hasattr(ctx, "proc") and ctx.proc.userdata.get("lead_id"):
                lead_id = ctx.proc.userdata["lead_id"]
        except Exception:
            pass

    if not lead_id:
        try:
            # 2. Check form state fields
            fields = {}
            if ctx:
                try:
                    from tools.form_control import _get_state
                    state = _get_state(ctx)
                    fields = state.get("fields", {})
                except Exception:
                    pass

            phone = fields.get("lead_phone")
            name = fields.get("lead_name")
            email = fields.get("lead_email")

            # Try matching existing lead by phone
            if phone:
                res = db.table("leads").select("id").eq("phone", phone).order("created_at", desc=True).limit(1).execute()
                if res and res.data:
                    lead_id = res.data[0]["id"]
                    logger.info(f"[BrowserRec] Matched existing lead {lead_id} by phone {phone}")

            # Fallback: match most recently created lead
            if not lead_id:
                res = db.table("leads").select("id").order("created_at", desc=True).limit(1).execute()
                if res and res.data:
                    lead_id = res.data[0]["id"]
                    logger.info(f"[BrowserRec] Matched most recent lead {lead_id}")

            # Final fallback: create new lead for browser call
            if not lead_id:
                new_lead = {
                    "name": name or "Web Visitor",
                    "phone": phone or None,
                    "email": email or None,
                    "lead_status": "communication",
                    "call_status": "completed",
                }
                res = db.table("leads").insert(new_lead).execute()
                if res and res.data:
                    lead_id = res.data[0]["id"]
                    logger.info(f"[BrowserRec] Created new lead {lead_id} for browser call")
        except Exception as resolve_err:
            logger.error(f"[BrowserRec] Error resolving lead_id: {resolve_err}")

    if not lead_id:
        logger.warning("[BrowserRec] Could not resolve or create lead_id — skipping save.")
        return None

    call_duration_secs = max(0, int(time.time() - call_start_ts))

    # ── Step 1: Stop Egress, wait for finalization, download if needed ─────────
    # Two modes:
    #   A) recording_path is pre-set — Egress configured with S3Upload (direct Supabase write).
    #      We just stop the egress and wait for encoding to finish.
    #   B) recording_path is None — plain egress. Download file from LiveKit, upload via HTTP.
    if egress_id:
        try:
            lk_api = LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET"),
            )
            async with lk_api:
                await lk_api.egress.stop_egress(StopEgressRequest(egress_id=egress_id))
                logger.info(f"[BrowserRec] Egress {egress_id} stopped. Waiting for finalization...")
                # Poll for completion (up to 30s)
                egress_info = None
                for _ in range(6):
                    await asyncio.sleep(5)
                    list_req = await lk_api.egress.list_egress(
                        ListEgressRequest(egress_id=egress_id)
                    )
                    if list_req.items:
                        egress_info = list_req.items[0]
                        if egress_info.status.value >= 3:  # 3=COMPLETE, 4=FAILED
                            break

                if egress_info and egress_info.status.value == 3:  # COMPLETE
                    if recording_path:
                        # Mode A: already uploaded to Supabase via S3 — just log
                        logger.info(f"[BrowserRec] Recording finalized at s3://call-recordings/{recording_path}")
                    else:
                        # Mode B: download from LiveKit and upload to Supabase via HTTP
                        download_url = None
                        for fr in (egress_info.file_results or []):
                            download_url = getattr(fr, "download_url", None) or getattr(fr, "location", None)
                            if download_url:
                                break
                        if download_url:
                            logger.info(f"[BrowserRec] Downloading egress file from LiveKit: {download_url[:80]}...")
                            async with httpx.AsyncClient(timeout=120.0) as dl_client:
                                resp = await dl_client.get(download_url)
                                if resp.status_code == 200:
                                    supabase_url = os.getenv("SUPABASE_URL")
                                    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
                                    storage_path = f"{lead_id}/{room_name}.ogg"
                                    upload_resp = await dl_client.post(
                                        f"{supabase_url}/storage/v1/object/call-recordings/{storage_path}",
                                        content=resp.content,
                                        headers={
                                            "Authorization": f"Bearer {service_key}",
                                            "Content-Type": "audio/ogg",
                                        },
                                    )
                                    if upload_resp.status_code in (200, 201):
                                        recording_path = storage_path
                                        logger.info(f"[BrowserRec] Uploaded recording to call-recordings/{storage_path}")
                                    else:
                                        logger.warning(f"[BrowserRec] Upload failed: {upload_resp.status_code}")
                elif egress_info:
                    logger.warning(f"[BrowserRec] Egress ended with status {egress_info.status} — recording may be incomplete")
                    recording_path = None
        except Exception as eg_err:
            logger.error(f"[BrowserRec] Egress stop/finalize error: {eg_err}", exc_info=True)
            recording_path = None

    # ── Step 2: Build transcript from conversation_log ──────────────────────
    transcript_lines = []
    for item in conversation_log:
        try:
            role = getattr(item, "role", None)
            # chat_ctx items have .content which can be list of ContentPart or str
            raw = getattr(item, "content", "")
            if isinstance(raw, list):
                text = " ".join(
                    getattr(p, "text", "") for p in raw
                    if getattr(p, "text", "")
                )
            else:
                text = str(raw)
            if text and role in ("user", "assistant"):
                label = "Customer" if role == "user" else "Priya"
                transcript_lines.append(f"{label}: {text.strip()}")
        except Exception:
            pass

    transcript = "\n".join(transcript_lines)

    # ── Step 3: Save to Supabase ────────────────────────────────────────────
    try:
        from context_loader import preload_lead  # already imported at module level
        db = preload_lead.__globals__["_get_supabase"]()

        update: dict = {
            "browser_call_at": datetime.now(timezone.utc).isoformat(),
            "browser_call_duration_secs": call_duration_secs,
        }
        if transcript:
            update["browser_call_transcription"] = transcript
        if recording_path:
            update["browser_call_recording_url"] = recording_path

        db.table("leads").update(update).eq("id", lead_id).execute()
        logger.info(
            f"[BrowserRec] Saved browser call data for lead {lead_id} — "
            f"recording={'yes' if recording_path else 'no'}, "
            f"transcript_lines={len(transcript_lines)}, duration={call_duration_secs}s"
        )
    except Exception as db_err:
        logger.error(f"[BrowserRec] Failed to save to Supabase: {db_err}", exc_info=True)

    return lead_id


# ─── Pre-warm: load Silero VAD model once per worker process ──────────────────

def prewarm(proc: JobProcess):
    """Load heavy models ONCE when the worker process starts — never per-call."""
    # 1. Silero VAD (used for end-of-speech detection)
    logger.info("Prewarming Silero VAD model...")
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=config.VAD_MIN_SILENCE_DURATION,
        activation_threshold=config.VAD_ACTIVATION_THRESHOLD,
        # 8kHz = half the audio data per chunk vs 16kHz, so silero inference
        # completes in realtime on CPU (fixes 'inference is slower than realtime' warning).
        # The silero model is trained on both 8 kHz and 16 kHz so VAD accuracy is
        # unaffected for speech/silence detection purposes.
        sample_rate=8000,
        # min_speech_duration: ignore noise bursts shorter than 100ms (reduces false positives)
        min_speech_duration=0.1,
    )
    logger.info("Silero VAD loaded.")

    # 2. Policy search: use warm HTTP RAG service (port 8005) instead of loading
    # heavy PyTorch/SentenceTransformer models inside voice worker process.
    # Disabling in-process prewarm thread prevents 100% CPU spikes during call setup
    # which was causing ElevenLabs audio streaming to stutter / speak gibberish.
    logger.info("Policy search configured to use persistent HTTP RAG service on port 8005.")


# ─── Agent class ─────────────────────────────────────────────────────────────

class StarHealthAgent(Agent):
    """
    Star Health Insurance voice advisor.
    - Greets the customer by name on call entry.
    - Has full lead profile + memories pre-loaded (no per-turn DB calls on common path).
    - Uses function tools for memory, policy RAG, and WhatsApp.
    """

    def __init__(
        self,
        *,
        chat_ctx: ChatContext,
        lead: dict,
        memories: list,
        is_voip: bool = False,
        is_form_mode: bool = False,
        is_advisor_room: bool = False,
    ) -> None:
        lang = lead.get("language") or lead.get("preferred_language") or "hi"
        if is_advisor_room:
            instructions = build_system_prompt(lead, memories, language=lang)
        elif is_form_mode:
            instructions = build_form_assistant_prompt()
        else:
            instructions = build_system_prompt(lead, memories, language=lang)
        chat_ctx.add_message(role="system", content=instructions)
        super().__init__(chat_ctx=chat_ctx, instructions=instructions)
        self._lead = lead
        self._memories = memories
        self._is_voip = is_voip
        self._is_form_mode = is_form_mode
        self._is_advisor_room = is_advisor_room
        self._initial_step = 1  # will be updated by initial_step_context packet
        self._advisor_mode_active = is_advisor_room  # True from start in advisor-room mode

    async def switch_to_advisor_mode(self, plan_id: str, plan_name: str, monthly_premium: str) -> None:
        """
        Called when recommendation_loaded is received from the browser.
        Permanently replaces the form-filling instructions with the full advisor prompt
        (build_system_prompt) so the LLM has the customer's profile, plan details,
        and correct persona for the rest of the conversation.
        """
        if self._advisor_mode_active:
            logger.info("switch_to_advisor_mode called but already in advisor mode — skipping.")
            return

        self._advisor_mode_active = True
        self._is_form_mode = False

        # Build full advisor prompt with lead & memory context
        lang = self._lead.get("language") or self._lead.get("preferred_language") or "hi"
        advisor_instructions = build_system_prompt(self._lead, self._memories, language=lang)


        # Update the agent's default options instructions
        self.update_options(instructions=advisor_instructions)

        # Directly update the active chat context's top system message (replace form prompt with advisor prompt)
        updated_system = False
        for msg in self.chat_ctx.items:
            if msg.role == "system":
                msg.content = advisor_instructions
                updated_system = True
                logger.info("Successfully replaced system prompt in chat context with advisor instructions.")
                break
        if not updated_system:
            self.chat_ctx.add_message(role="system", content=advisor_instructions)

        # Inject a clear role-switch system message at the top of the context
        lead_name = self._lead.get("name", "the customer")
        premium_words = f"{monthly_premium} Rupees per month" if monthly_premium else "to be confirmed"
        self.chat_ctx.add_message(
            role="system",
            content=(
                f"[ROLE SWITCH — PERMANENT]: You are now a Star Health Insurance Policy Advisor speaking to {lead_name}. "
                f"Their recommended plan is '{plan_name}' (ID: {plan_id}). Monthly premium: {premium_words}. "
                f"You MUST start by discussing and explaining '{plan_name}'. "
                f"Focus exclusively on '{plan_name}'. Do NOT pitch or suggest any other plans unless the customer explicitly asks for a comparison. "
                f"NEVER ask any form-filling questions. "
                f"Use search_policies() to answer any plan questions silently in the background.]"
            )
        )
        logger.info(f"Advisor mode activated for plan: {plan_id} ({plan_name}), premium: {monthly_premium}")

    async def on_enter(self) -> None:
        """Generate the opening as soon as the agent joins the room."""

        # ── Advisor-room: speak greeting directly via TTS (bypasses LLM) ────────
        if self._is_advisor_room:
            name = self._lead.get("name", "")
            first_name_raw = name.split()[0] if name else ""
            first_name = config.COMMON_NAMES_MAP.get(first_name_raw, first_name_raw)
            recommended_plan = (
                self._lead.get("recommended_plan_id")
                or self._lead.get("recommended_plan")
                or self._lead.get("recommendedPlan")
                or ""
            )
            plan_hi = config.PLAN_NAME_MAP.get(recommended_plan, recommended_plan) or "यंग स्टार"
            address = f"{first_name} जी" if first_name else ""
            greeting_text = (
                f"नमस्ते{' ' + address if address else ''}! मैं प्रिया हूँ स्टार हेल्थ से — "
                f"आपके लिए {plan_hi} recommend किया है। आप इसके बारे में जानना चाहेंगे?"
            )
            await self.session.say(greeting_text)
            return



        # ── Form-filling assistant mode ───────────────────────────────────────
        if self._is_form_mode:
            initial_step = getattr(self, "_initial_step", 1)
            if initial_step <= 1:
                # Fresh start — greet and ask the opening question
                await self.session.generate_reply(
                    instructions=(
                        "NO FILLER. Start immediately with exactly: "
                        "'हाय, मैं प्रिया हूँ स्टार हेल्थ से।' "
                        "Then immediately ask: "
                        "'आप किसको cover करना चाहेंगे — सिर्फ आप, या family भी cover करनी है?' "
                        "No other text. No intro. No welcome speech. No fillers."
                    )
                )
            else:
                # Mid-form: user already filled some steps — start contextually
                step_names = {
                    2: "Medical History (Diabetes, Pregnancy, Pre-existing conditions)",
                    3: "Location and Budget (City type and budget range)",
                    4: "Employer and Hospital preferences",
                    5: "Contact Details (Name, phone, email)",
                }
                step_label = step_names.get(initial_step, f"Step {initial_step}")
                await self.session.generate_reply(
                    instructions=(
                        f"The customer has already filled the earlier steps and is currently on Step {initial_step}: {step_label}. "
                        f"Do NOT introduce yourself with 'Haaय, मैं प्रिया हूँ' or repeat any previous steps. "
                        f"Say a short 1-sentence greeting acknowledging you are here to help, "
                        f"then immediately ask the FIRST question for Step {initial_step}."
                    )
                )
            return

        # ── Standard advisor mode: speak greeting directly via TTS (bypasses LLM) ─
        # session.say() sends text straight to TTS — no LLM, no filler rule applied,
        # no risk of hallucination or off-script response.
        name = self._lead.get("name", "")
        first_name_raw = name.split()[0] if name else ""
        first_name = config.COMMON_NAMES_MAP.get(first_name_raw, first_name_raw)
        recommended_plan = (
            self._lead.get("recommended_plan_id")
            or self._lead.get("recommended_plan")
            or self._lead.get("recommendedPlan")
            or ""
        )
        plan_hi = config.PLAN_NAME_MAP.get(recommended_plan, recommended_plan) or "your plan"
        address = f"{first_name} जी" if first_name else ""

        if self._is_voip:
            greeting_text = (
                f"नमस्ते{' ' + address if address else ''}! "
                f"मैं प्रिया हूँ स्टार हेल्थ से — "
                f"आपके लिए {plan_hi} recommend है, aap iske bare me janna chahenge?"
            )
        else:
            greeting_text = (
                f"नमस्ते{' ' + address if address else ''}! "
                f"मैं प्रिया हूँ स्टार हेल्थ से — "
                f"आपके लिए {plan_hi} recommend है, aap iske bare me janna chahenge?"
            )

        await self.session.say(greeting_text)



    async def tts_node(
        self, text: AsyncIterable[str], model_settings: Any
    ):
        """
        Phrase-level TTS buffering with pre-TTS sanitization.

        sanitize_for_tts() runs BEFORE ElevenLabs receives text — critical because
        transcription_node only sanitizes the displayed transcript, not the audio stream.
        ElevenLabs must never receive raw function names, snake_case, or bracket tags.
        """
        _SNAKE_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z0-9_]+\b')  # any snake_case
        _BRACKET_RE = re.compile(r'\[[^\]]{1,30}\]')                         # [bracket tags]
        _META_RE = re.compile(
            r'\b(function|query\s*=|tool_call|function_call|parameters?\s*[:=]).*',
            re.IGNORECASE
        )
        # Devanagari digits → ASCII (handles ०-৯ that ElevenLabs can't speak)
        _DEVA_DIGIT_TABLE = str.maketrans('०१२३४५६७८९', '0123456789')
        # All 8 plan prices: Hindi words → English words (mathematically verified)
        # Ordered longest-match first to prevent partial substring matches
        _HINDI_PRICE_MAP = [
            # 2,299 — Super Star
            ('दो हजार दो सौ निन्यानवे',   'two thousand two hundred ninety nine'),
            # 1,899 — Star Premier
            ('एक हजार आठ सौ निन्यानवे',   'one thousand eight hundred ninety nine'),
            # 1,499 — Star Assure
            ('एक हजार चार सौ निन्यानवे',  'one thousand four hundred ninety nine'),
            # 1,199 — Family Health Optima
            ('एक हजार एक सौ निन्यानवे',   'one thousand one hundred ninety nine'),
            # 1,099 — Star Comprehensive
            ('एक हजार निन्यानवे',          'one thousand ninety nine'),
            # 899 — Medi Classic
            ('आठ सौ निन्यानवे',            'eight hundred ninety nine'),
            # 799 — Arogya Sanjeevani
            ('सात सौ निन्यानवे',           'seven hundred ninety nine'),
            # 699 — Young Star
            ('छह सौ निन्यानवे',            'six hundred ninety nine'),
            # Coverage amounts — use explicit English words so ElevenLabs pronounces "five Lakh" (not "panch")
            ('पचास लाख',   'fifty Lakh'),
            ('पच्चीस लाख', 'twenty five Lakh'),
            ('दस लाख',     'ten Lakh'),
            ('पाँच लाख',   'five Lakh'),
            ('पांच लाख',   'five Lakh'),
            ('दो करोड़',   'two Crore'),
            ('एक करोड़',   'one Crore'),
        ]

        def _num_to_words(n: int) -> str:
            """Convert integer 1–9999 to spoken English words."""
            ones = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
                    'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen',
                    'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen']
            tens = ['', '', 'twenty', 'thirty', 'forty', 'fifty',
                    'sixty', 'seventy', 'eighty', 'ninety']
            if n < 20:
                return ones[n]
            if n < 100:
                return tens[n // 10] + (' ' + ones[n % 10] if n % 10 else '')
            if n < 1000:
                rest = n % 100
                return ones[n // 100] + ' hundred' + (' ' + _num_to_words(rest) if rest else '')
            rest = n % 1000
            return ones[n // 1000] + ' thousand' + (' ' + _num_to_words(rest) if rest else '')

        _PRICE_BEFORE_RUPEES = re.compile(
            r'(\d(?:\s+\d)+|\d{1,4}(?:,\d{3})*)(?=\s+[Rr]upees|\s+RUPEES)'
        )
        _LAKH_RE = re.compile(r'\b(\d+)\s*(?:lakh|Lakh|लाख)\b', re.IGNORECASE)
        _CRORE_RE = re.compile(r'\b(\d+)\s*(?:crore|Crore|करोड़)\b', re.IGNORECASE)
        _MONTH_RE = re.compile(r'\b(\d+)\s*(?:per month|monthly|प्रति माह|प्रतिमाह)\b', re.IGNORECASE)

        def _clean_text(text: str) -> str:
            """Strip forbidden patterns and convert numbers to TTS-safe English."""
            # Devanagari digits → ASCII first
            text = text.translate(_DEVA_DIGIT_TABLE)
            # Currency symbols / prefixes → standard words
            text = text.replace('₹', '').replace('Rs.', 'Rupees').replace('Rs ', 'Rupees ')
            # Hindi price-number words → English words (longest match first)
            for hindi, english in _HINDI_PRICE_MAP:
                text = text.replace(hindi, english)
            # Convert numeric digits before Lakh/Crore/Month into spoken English words
            text = _LAKH_RE.sub(lambda m: f"{_num_to_words(int(m.group(1)))} Lakh", text)
            text = _CRORE_RE.sub(lambda m: f"{_num_to_words(int(m.group(1)))} Crore", text)
            text = _MONTH_RE.sub(lambda m: f"{_num_to_words(int(m.group(1)))} per month", text)
            # Convert Hindi rupee/time words → English
            text = (text
                    .replace('रुपये', 'Rupees')
                    .replace('रुपया', 'Rupees')
                    .replace('रुपए', 'Rupees')
                    .replace('प्रति माह', 'per month')
                    .replace('प्रतिमाह', 'per month'))
            # Rejoin spaced digits before Rupees: "6 9 9 Rupees" → "six hundred ninety nine Rupees"
            def _rejoin_and_speak(m: re.Match) -> str:
                raw = m.group(1).replace(' ', '').replace(',', '')
                try:
                    return _num_to_words(int(raw))
                except ValueError:
                    return raw
            text = _PRICE_BEFORE_RUPEES.sub(_rejoin_and_speak, text)
            text = _BRACKET_RE.sub("", text)
            text = _SNAKE_RE.sub("", text)
            text = _META_RE.sub("", text)
            return text





        async def _sanitize_for_tts(stream: AsyncIterable[str]) -> AsyncIterable[str]:
            buf = ""
            async for chunk in stream:
                if not isinstance(chunk, str):
                    if buf.strip():
                        yield _clean_text(buf).strip()
                        buf = ""
                    yield chunk
                    continue
                buf += chunk
                buf = _clean_text(buf)
                # Emit on natural phrase boundaries: space, newline, or trailing punctuation
                # This ensures fillers like "हाँ देखिए," reach ElevenLabs immediately
                ends_on_punct = buf.rstrip().endswith((',', '!', '?', '।', ':', ';'))
                has_space = " " in buf or "\n" in buf
                if not has_space and not ends_on_punct:
                    continue
                if ends_on_punct and not has_space:
                    # Short filler or punctuated phrase — emit entire buffer immediately
                    to_emit = buf.strip()
                    buf = ""
                else:
                    parts = buf.rsplit(" ", 1)
                    to_emit = parts[0].strip()
                    buf = parts[1] if len(parts) == 2 else ""
                if to_emit:
                    yield to_emit + " "
            # Flush remainder
            remainder = _clean_text(buf).strip()
            if remainder:
                yield remainder

        # ElevenLabs streams natively — skip the PhraseTokenizer wrapper
        if config.TTS_PROVIDER == "elevenlabs":
            try:
                async for frame in Agent.default.tts_node(self, _sanitize_for_tts(text), model_settings):
                    yield frame
                return
            except Exception as e:
                logger.warning(f"ElevenLabs TTS connection issue ({e}). Gracefully recovering audio stream.")
                return

        from livekit.agents import tts
        from livekit.agents.utils import aio

        wrapped_tts = tts.StreamAdapter(
            tts=self.session.tts,
            sentence_tokenizer=PhraseTokenizer(
                min_words_soft=8,   # flush at comma/colon after 8+ words
                max_words=20,       # force flush if no punctuation in 20 words
            ),
        )

        conn_options = self.session.conn_options.tts_conn_options
        async with wrapped_tts.stream(conn_options=conn_options) as stream:
            async def _forward_input():
                async for chunk in text:
                    stream.push_text(chunk)
                stream.end_input()

            forward_task = asyncio.create_task(_forward_input())
            try:
                async with asyncio.timeout(15): # prevent indefinite hang
                    async for ev in stream:
                        yield ev.frame
            except asyncio.TimeoutError:
                logger.warning("TTS stream timeout reached")
            finally:
                await aio.cancel_and_wait(forward_task)

    def _get_content_text(self, m) -> str:
        content = getattr(m, "content", "")
        if not content:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif hasattr(part, "text"):
                    parts.append(part.text)
                else:
                    parts.append(str(part))
            return "".join(parts)
        return str(content)

    async def llm_node(
        self,
        chat_ctx,
        tools,
        model_settings,
    ):
        """
        Prune conversation history before each LLM call by keeping only the last
        N complete turns, and pruning old system notifications to reduce token overhead
        and cost on Groq billing.
        """
        logger.info(f"[LLM Node] chat_ctx.items count: {len(chat_ctx.items)}")
        if logger.isEnabledFor(logging.DEBUG):
            for idx, m in enumerate(chat_ctx.items):
                role = getattr(m, "role", "unknown")
                content = self._get_content_text(m)
                content_snippet = (content[:50] + "...") if content and len(content) > 50 else content
                logger.debug(f"  Item {idx}: role={role}, type={type(m)}, content={content_snippet}")

        # Form filling needs very short context (3 turns); advisor needs more context for better 70B responses (6 turns)
        MAX_TURNS = 3 if self._is_form_mode else 6

        # 1. Filter out old system notifications and keep only ONE latest primary persona prompt
        system_msgs = []
        for m in reversed(chat_ctx.items):
            role = getattr(m, "role", None)
            if role == "system":
                m_content = self._get_content_text(m)
                if "You are Priya" in m_content or "ROLE SWITCH" in m_content:
                    system_msgs.append(m)
                    break  # Keep ONLY the single latest system prompt

        # 2. Extract conversation turns chronologically, keeping tool calls/outputs associated with their turn
        turns = []
        current_turn = []
        for m in chat_ctx.items:
            role = getattr(m, "role", None)
            if role == "system":
                continue
            if m.__class__.__name__ == "AgentConfigUpdate":
                continue

            if role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [m]
            else:
                if current_turn:
                    current_turn.append(m)
                else:
                    # Pre-turn messages (like greeting or pre-greeting tools) are kept as system/priming messages
                    system_msgs.append(m)

        if current_turn:
            turns.append(current_turn)

        # 3. Dynamic Form State Injection (only for form mode)
        if self._is_form_mode:
            try:
                from tools.form_control import _get_state
                class DummyCtx:
                    def __init__(self, userdata):
                        self.userdata = userdata
                dummy_ctx = DummyCtx(self.session.userdata)
                state = _get_state(dummy_ctx)
                
                current_step = state.get("step", 1)
                # Keep only fields that actually have values filled
                filled_fields = {
                    k: v for k, v in state.get("fields", {}).items() 
                    if v not in (None, "", False, 0, [])
                }
                
                state_msg = (
                    f"[System Notification: The customer is currently looking at Step {current_step} of the form. "
                    f"The currently filled form fields are: {json.dumps(filled_fields)}. "
                    f"Do NOT ask any questions for fields that are already filled. "
                    f"Immediately proceed to the first unanswered field on Step {current_step}. "
                    f"If all fields on Step {current_step} are filled and confirmed, call advance_form_step() once to move to the next step.]"
                )
                
                ChatMessageClass = None
                for m in chat_ctx.items:
                    if m.__class__.__name__ == "ChatMessage":
                        ChatMessageClass = m.__class__
                        break
                if ChatMessageClass:
                    state_msg_obj = ChatMessageClass(role="system", content=[state_msg])
                    system_msgs.append(state_msg_obj)
                    logger.info(f"[LLM Node Debug] Injected dynamic state message: {state_msg}")
            except Exception as e:
                logger.error(f"Error injecting dynamic form state: {e}")

        # 4. Prune the session's chat context in-place so it persists across turns
        chat_ctx.items.clear()
        chat_ctx.items.extend(system_msgs)
        if len(turns) > MAX_TURNS:
            pruned_conv = []
            for turn in turns[-MAX_TURNS:]:
                pruned_conv.extend(turn)
            chat_ctx.items.extend(pruned_conv)
            logger.info("History pruned in-place to %d complete turns (%d messages)", MAX_TURNS, len(pruned_conv))
        else:
            pruned_conv = []
            for turn in turns:
                pruned_conv.extend(turn)
            chat_ctx.items.extend(pruned_conv)

        logger.info(f"[LLM Node] Final chat_ctx items count: {len(chat_ctx.items)}")
        if logger.isEnabledFor(logging.DEBUG):
            for idx, m in enumerate(chat_ctx.items):
                role = getattr(m, "role", "unknown")
                content = self._get_content_text(m)
                content_snippet = (content[:50] + "...") if content and len(content) > 50 else content
                logger.debug(f"  Final Item {idx}: role={role}, type={type(m)}, content={content_snippet}")

        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)

    # _classify_intent_filler removed — filler is now driven by prompt rule in
    # context_loader.build_system_prompt() (Approach B). The LLM handles filler
    # selection naturally based on the RESPONSE OPENER rule in the system prompt.

    async def transcription_node(
        self, text, model_settings
    ):
        """
        Sanitise LLM output before it reaches Sarvam TTS.

        Even though the system prompt forbids markdown, the LLM occasionally
        emits stray * ** # or numbered-list markers. These characters are read
        aloud verbatim by TTS ("star", "hash", "one dot") and ruin prosody.
        Strip them here so TTS always receives clean conversational text.
        """
        _MARKDOWN_RE = re.compile(
            r"\*{1,2}|#{1,6}\s?|\d+\.\s|^[-•]\s", re.MULTILINE
        )

        async def _clean(stream):
            # BANNED_WORDS: exact snake_case tokens to suppress (also caught by FUNC_NAME_RE below)

            BANNED_WORDS = {
                "send_whatsapp_details",
                "search_policies",
                "remember_detail",
                "recall_detail",
                "policy_name",
                "whatsapp_details",
                "details_label",
                "memory_type",
                "form_update",
                "form_advance",
                "form_submit",
                "update_form_field",
                "advance_form_step",
                "submit_form",
                "go_to_form_step",
                "form_go_to_step",
                "parameter",
                "function",
                "tool",
                "update",
                "field",
                "true",
                "false",
            }

            # Hard regex: wipe any snake_case function call pattern before it reaches TTS
            FUNC_NAME_RE = re.compile(
                r"\b(update_form_field|advance_form_step|submit_form|go_to_form_step"
                r"|search_policies|remember_detail|recall_detail|send_whatsapp_details"
                r"|form_update|form_advance|form_submit|form_go_to_step)\b",
                re.IGNORECASE
            )

            # Direct word replacements to translate numbers and units to spoken Hindi text
            WORD_REPLACEMENTS = {
                # Premiums
                "699": "छह सौ निन्यानवे",
                "799": "सात सौ निन्यानवे",
                "899": "आठ सौ निन्यानवे",
                "1099": "एक हजार निन्यानवे",
                "1199": "एक हजार एक सौ निन्यानवे",
                "1499": "एक हजार चार सौ निन्यानवे",
                "1899": "एक हजार आठ सौ निन्यानवे",
                "1999": "एक हजार नौ सौ निन्यानवे",
                "2299": "दो हजार दो सौ निन्यानवे",
                "2499": "दो हजार चार सौ निन्यानवे",
                "1,099": "एक हजार निन्यानवे",
                "1,199": "एक हजार एक सौ निन्यानवे",
                "1,499": "एक हजार चार सौ निन्यानवे",
                "1,899": "एक हजार आठ सौ निन्यानवे",
                "1,999": "एक हजार नौ सौ निन्यानवे",
                "2,299": "दो हजार दो सौ निन्यानवे",
                "2,499": "दो हजार चार सौ निन्यानवे",
                "lakh": "लाख",
                "lakhs": "लाख",
                "crore": "करोड़",
                "crores": "करोड़",
                "rupee": "रुपये",
                "rupees": "रुपये",
                "18": "अठारह",
                "40": "चालीस",
                "50": "पचास",
                "50+": "पचास से ज्यादा",
                "9": "नौ",
                "5": "पांच",
                "10": "दस",
                "15": "पंद्रह",
                "25": "पच्चीस",
                "1": "एक",
                "2": "दो",
            }

            def apply_replacements(word: str) -> str:
                w = word
                if "₹" in w:
                    w = w.replace("₹", "").replace("$", "").strip()
                    if w and not w.lower().endswith("rupees"):
                        w = w + " Rupees"
                    word = w
                word_clean = word.replace("₹", "").replace("$", "")
                match = re.match(r"^([^\w]*)(.*?)([^\w]*)$", word_clean)
                if not match:
                    return word_clean
                lead_punc, core, tail_punc = match.groups()
                core_lower = core.lower()
                if core_lower in WORD_REPLACEMENTS:
                    return lead_punc + WORD_REPLACEMENTS[core_lower] + tail_punc
                return word_clean

            buffer = ""
            async for chunk in stream:
                if not isinstance(chunk, str):
                    if buffer.strip():
                        yield buffer
                        buffer = ""
                    yield chunk
                    continue

                buffer += chunk

                # Strip [bracket tags] like [pause], [soft chuckle] before they reach TTS
                buffer = re.sub(r'\[[^\]]{1,30}\]', '', buffer)

                # Hard-wipe any function name / banned phrase in the accumulated buffer
                buffer = FUNC_NAME_RE.sub("", buffer)

                # Only process when we hit a word boundary
                if not any(c in chunk for c in (" ", "\t", "\n")):
                    continue

                # Clean markdown / backticks
                cleaned = _MARKDOWN_RE.sub("", buffer).replace("`", "")
                words = cleaned.split()
                if not words:
                    continue

                # Hold the last word if it may be incomplete
                last_incomplete = not buffer[-1].isspace()
                words_to_emit = words[:-1] if (last_incomplete and len(words) > 1) else words
                buffer = words[-1] if (last_incomplete and len(words) > 1) else ""

                for w in words_to_emit:
                    w_out = apply_replacements(w)
                    chk = re.sub(r"[^\w_]", "", w_out).lower()
                    # Skip standalone banned words and any residual snake_case
                    if "_" in chk or chk in BANNED_WORDS:
                        continue
                    yield w_out + " "

            # Flush any remaining buffer
            if buffer.strip():
                buffer = FUNC_NAME_RE.sub("", buffer)
                chk = re.sub(r"[^\w_]", "", buffer).lower()
                if "_" not in chk and chk not in BANNED_WORDS:
                    yield buffer

        return _clean(Agent.default.transcription_node(self, text, model_settings))


async def entrypoint(ctx: JobContext):
    """
    Main entrypoint. Called by LiveKit for every incoming job (call).
    Parses metadata, pre-loads lead context, starts the agent session.
    """
    await ctx.connect()
    logger.info(f"📞 New call connected to room: {ctx.room.name}")

    # ── Parse metadata (sent by make_call.py or the web dispatch) ──────────────
    metadata: dict = {}
    try:
        raw = ctx.job.metadata or "{}"
        metadata = json.loads(raw)
    except Exception:
        logger.warning(f"Could not parse job metadata: {ctx.job.metadata!r}")

    lead_id = metadata.get("lead_id") or metadata.get("leadId")
    if lead_id in ("undefined", "null", "anonymous", ""):
        lead_id = None

    # ── Parse room metadata (sent by room creator / make_call.py) ──────────────
    if not lead_id and ctx.room.metadata:
        try:
            room_meta = json.loads(ctx.room.metadata)
            lead_id = room_meta.get("lead_id") or room_meta.get("leadId")
            if lead_id:
                logger.info(f"Resolved lead_id from room metadata: {lead_id}")
        except Exception as e:
            logger.warning(f"Could not parse room metadata: {e}")

    # ── WebRTC Inbound Call Fallback: Resolve lead_id from room name or participant identity/metadata ──
    if not lead_id:
        if ctx.room.name.startswith("browser-room-"):
            room_parts = ctx.room.name.split("-")
            if len(room_parts) >= 4:
                lead_id = "-".join(room_parts[2:-1])
                logger.info(f"Resolved lead_id from browser room name: {lead_id}")
        elif ctx.room.name.startswith("call-"):
            room_parts = ctx.room.name.split("-")
            if len(room_parts) >= 7:
                # UUID format: call-123e4567-e89b-12d3-a456-426614174000-timestamp
                lead_id = "-".join(room_parts[1:-1])
                logger.info(f"Resolved lead_id from outbound call room name: {lead_id}")
            elif len(room_parts) >= 3:
                # Simple non-UUID format: call-leadId-timestamp
                lead_id = "-".join(room_parts[1:-1])
                logger.info(f"Resolved lead_id from simple call room name: {lead_id}")

    if not lead_id:
        for identity, participant in ctx.room.remote_participants.items():
            if identity.startswith("customer-"):
                try:
                    p_meta = json.loads(participant.metadata or "{}")
                    p_lead_id = p_meta.get("lead_id") or p_meta.get("leadId")
                    if p_lead_id and p_lead_id not in ("undefined", "null", "anonymous", ""):
                        lead_id = p_lead_id
                        logger.info(f"Resolved lead_id from participant metadata: {lead_id}")
                        break
                except Exception:
                    pass

                # Strip customer- prefix to get the full UUID or ID (e.g. customer-UUID)
                lead_id = identity[len("customer-"):]
                logger.info(f"Resolved lead_id from participant identity: {lead_id}")
                break

    # ── Pre-load lead + memories (once per call, no per-turn overhead) ─────────
    lead, memories = await preload_lead(lead_id)

    # ── Extract plan_id from participant metadata if passed ────────────────────
    for identity, participant in ctx.room.remote_participants.items():
        try:
            p_meta = json.loads(participant.metadata or "{}")
            if p_meta.get("plan_id"):
                if not lead:
                    lead = {}
                lead["recommended_plan_id"] = p_meta["plan_id"]
                logger.info(f"Resolved recommended_plan_id from participant metadata: {p_meta['plan_id']}")
        except Exception:
            pass

    # ── Detect room type ────────────────────────────────────────────────────────
    is_voip = ctx.room.name.startswith("browser-room-")
    is_form_mode = ctx.room.name.startswith("form-room-")
    is_advisor_room = ctx.room.name.startswith("advisor-room-")

    # ── Resolve recommended_plan_id from room name if in advisor room ────────────
    if is_advisor_room and not lead.get("recommended_plan_id"):
        room_sub = ctx.room.name[len("advisor-room-"):]
        for p_id in config.PLAN_NAME_MAP.keys():
            if room_sub.startswith(p_id):
                if not lead:
                    lead = {}
                lead["recommended_plan_id"] = p_id
                logger.info(f"Resolved recommended_plan_id from room name: {p_id}")
                break


    # ── Store context in userdata so tools can access it ───────────────────────
    ctx.proc.userdata["lead_id"] = lead_id
    ctx.proc.userdata["lead"] = lead

    # ── Build chat context (system prompt injected here) ───────────────────────
    chat_ctx = ChatContext()

    agent = StarHealthAgent(
        chat_ctx=chat_ctx,
        lead=lead,
        memories=memories,
        is_voip=is_voip,
        is_form_mode=is_form_mode,
        is_advisor_room=is_advisor_room,
    )

    # ── Configure the session ──────────────────────────────────────────────────
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(
        min_silence_duration=config.VAD_MIN_SILENCE_DURATION,
        activation_threshold=config.VAD_ACTIVATION_THRESHOLD,
        sample_rate=8000,
        min_speech_duration=0.1,
    )

    if config.TTS_PROVIDER == "elevenlabs":
        if elevenlabs is None:
            raise ImportError("livekit-plugins-elevenlabs is not installed or failed to import. Run pip install livekit-plugins-elevenlabs.")
        logger.info(
            f"Initializing ElevenLabs Expressive TTS: voice={config.ELEVENLABS_VOICE_ID} "
            f"model={config.ELEVENLABS_MODEL} stability={config.ELEVENLABS_STABILITY} style={config.ELEVENLABS_STYLE}"
        )
        voice_settings = elevenlabs.VoiceSettings(
            stability=config.ELEVENLABS_STABILITY,
            similarity_boost=config.ELEVENLABS_SIMILARITY,
            style=config.ELEVENLABS_STYLE,
            use_speaker_boost=False,  # Disabled: adds ~100-150ms server-side processing for minimal quality gain
        )
        tts_engine = elevenlabs.TTS(
            api_key=config.ELEVENLABS_API_KEY,
            voice_id=config.ELEVENLABS_VOICE_ID,
            model=config.ELEVENLABS_MODEL,
            voice_settings=voice_settings,
        )
    else:
        logger.info(f"Initializing Sarvam TTS with voice: {config.SARVAM_VOICE}")
        tts_engine = sarvam.TTS(
            model=config.SARVAM_MODEL,
            speaker=config.SARVAM_VOICE,
            target_language_code=config.SARVAM_LANGUAGE,
        )

    # ── Tool selection: form-mode gets form tools, advisor gets advisor tools ────
    if is_advisor_room:
        agent_tools = [search_policies, remember_detail, send_whatsapp_details]
        logger.info("Advisor-room mode: using advisor tools only (no form tools).")
    elif is_form_mode:
        agent_tools = [
            update_form_field,
            advance_form_step,
            submit_form,
            go_to_form_step,
            search_policies,
            remember_detail,
            send_whatsapp_details,
            get_current_form_state
        ]
        logger.info("Form-assistant mode: using all form + policy tools.")
    else:
        agent_tools = [search_policies, remember_detail, send_whatsapp_details]

    session = AgentSession(
        stt=deepgram.STT(
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            smart_format=False,
            interim_results=True,
            # endpointing_ms=100: Deepgram sends final transcript 100ms after silence.
            # Without this, Deepgram waits for more context before finalizing,
            # adding ~200ms extra latency per turn even though VAD already fired.
            endpointing_ms=100,
        ),
        llm=openai.LLM(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            model=config.GROQ_MODEL,
            temperature=config.GROQ_TEMPERATURE,
            max_completion_tokens=config.GROQ_MAX_TOKENS,
            # 30s read timeout: Groq streams tokens for up to ~5s on longer turns.
            # Default httpx read=5s causes 'Connection error' mid-stream which generates
            # stale/overlapping LLM responses heard as mid-call gibberish.
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            # parallel_tool_calls=False: LLaMA 70B generates invalid JSON when
            # batching multiple tool calls simultaneously. Groq rejects this with
            # "Failed to call a function" → wrapped as Connection error → 4 retries
            # → _llm_inference_task fails → stale response replays mid-call as gibberish.
            parallel_tool_calls=False,
        ),
        tts=tts_engine,
        vad=vad,
        tools=agent_tools,
        userdata={"lead_id": lead_id, "lead": lead, "room": ctx.room},
        max_tool_steps=5,
        # ── Ghost-utterance / mid-call gibberish fix ─────────────────────────────
        # ROOT CAUSE: LiveKit uses HYBRID turn detection (VAD + STT/eou). VAD fires
        # first and commits the turn. Deepgram then sends a LATE STT final which re-
        # triggers the eou module, causing a SECOND LLM inference on the same turn.
        # Both responses are TTS'd simultaneously → heard as gibberish mid-call.
        #
        # Fix 1 — turn_detection="vad": VAD-only mode. Late Deepgram finals are still
        # used for transcript display, but can NEVER trigger a new turn commit.
        #
        # Fix 2 — false_interruption_timeout=2.0: If a ghost somehow registers as
        # an interruption in the first 2 s of the agent speaking, it is discarded.
        #
        # Fix 3 — min_duration=0.25: User must speak ≥250 ms before it counts as a
        # real interruption, filtering noise bursts & sub-word ghost events.
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {
                "min_delay": 0.8,
                "max_delay": 3.0,
            },
            "interruption": {
                "false_interruption_timeout": 2.0,
                "min_duration": 0.25,
            },
        },
    )

    @ctx.room.on("data_received")
    def on_data_received(data_packet):
        try:
            payload = json.loads(data_packet.data.decode("utf-8"))
            if payload.get("type") == "user_navigation":
                step = payload.get("step")
                logger.info(f"Received user_navigation data packet from browser. Step: {step}")
                set_room_step(ctx.room.name, step)
                chat_ctx.add_message(
                    role="system",
                    content=(
                        f"[System Notification: Customer manually navigated to Step {step} in the UI. "
                        f"Acknowledge this transition, check the chat history to see what details were already collected for this step, "
                        f"and ask the customer what details they want to change or fill on Step {step}. "
                        f"Do NOT start the step from the beginning if details are already filled.]"
                    )
                )
                async def _do_nav_reply():
                    await session.generate_reply()
                asyncio.create_task(_do_nav_reply())
            elif payload.get("type") == "initial_step_context":
                step = payload.get("step")
                fields = payload.get("fields", {})
                logger.info(f"Received initial_step_context. Step: {step}, fields: {fields}")
                # Update the agent's known initial step
                agent._initial_step = step
                set_room_step(ctx.room.name, step)

                from tools.form_control import _get_state
                state = _get_state(ctx)
                if "fields" not in state:
                    state["fields"] = {}

                if step and int(step) > 1 and fields:
                    for k, v in fields.items():
                        if v not in (None, "", False, 0, []):
                            state["fields"][k] = v
                    logger.info(f"Synced {len(fields)} browser fields into server state for step {step}")

                # Inject a system message so the LLM knows the current context
                step_names = {
                    2: "Medical History",
                    3: "Location and Budget",
                    4: "Employer Insurance and Hospital",
                    5: "Contact Details",
                }
                step_label = step_names.get(step, f"Step {step}")
                chat_ctx.add_message(
                    role="system",
                    content=(
                        f"[System Notification: The customer started the voice assistant on Step {step}: {step_label}. "
                        f"The currently filled form fields are: {json.dumps(state['fields'])}. "
                        f"Do NOT introduce yourself with the Step 1 greeting if step > 1. "
                        f"Skip any fields/steps already completed. "
                        f"Greet briefly and immediately ask the first unanswered question for Step {step}.]"
                    )
                )
            elif payload.get("type") == "recommendation_loaded":
                plan_id = payload.get("planId", "")
                plan_name = payload.get("planName", plan_id)
                monthly_premium = payload.get("monthlyPremium", "")
                logger.info(f"Received recommendation_loaded. Plan: {plan_name} ({plan_id}), Premium: {monthly_premium}")

                async def _do_advisor_switch():
                    await agent.switch_to_advisor_mode(plan_id, plan_name, monthly_premium)
                    await session.generate_reply()

                asyncio.create_task(_do_advisor_switch())
            elif payload.get("type") == "manual_field_update":
                field = payload.get("field")
                value = payload.get("value")
                logger.info(f"Received manual_field_update from browser: {field} = {value}")
                
                # 1. Update the agent's preloaded lead object in userdata
                if agent._lead and isinstance(agent._lead, dict):
                    lead_key_map = {
                        "lead_name": "name",
                        "lead_phone": "phone",
                        "lead_email": "email",
                        "lead_gender": "gender"
                    }
                    k = lead_key_map.get(field, field)
                    agent._lead[k] = value
                
                # 2. Update the room state fields map + permanently lock this field
                from tools.form_control import _get_state
                state = _get_state(ctx)
                if "fields" not in state:
                    state["fields"] = {}
                state["fields"][field] = value
                # Mark this field as manually locked — update_form_field will reject
                # any agent attempt to overwrite it from this point forward
                if "manually_locked" not in state:
                    state["manually_locked"] = set()
                state["manually_locked"].add(field)
                logger.info(f"Field '{field}' marked as manually_locked in room state")

                # 3. Inject a strong system message so the LLM skips this field
                if field == "lead_phone":
                    sys_msg = (
                        f"[System Notification: The customer manually typed their phone number '{value}' directly on the screen. "
                        f"This is now LOCKED — do NOT call update_form_field('lead_phone', ...) ever again this session. "
                        f"Any attempt to overwrite it will be rejected. "
                        f"Accept '{value}' as the confirmed phone number and immediately move on to ask for their email (optional) or gender.]"
                    )
                else:
                    sys_msg = (
                        f"[System Notification: The customer manually updated form field '{field}' to '{value}' on their screen. "
                        f"This field is now LOCKED — do NOT call update_form_field for '{field}' again. "
                        f"Your internal record has been updated. Continue to the next question.]"
                    )
                chat_ctx.add_message(role="system", content=sys_msg)
        except Exception as e:
            logger.error(f"Error handling data packet: {e}")

    # ── Start the session ──────────────────────────────────────────────────────
    await session.start(agent=agent, room=ctx.room)
    logger.info("Agent session started. Waiting for call to end...")

    # ── Start LiveKit Egress recording for browser calls ───────────────────────
    # Only record browser/form rooms — SIP/telephony rooms are handled by Vobiz.
    egress_id: str | None = None
    browser_recording_path: str | None = None  # Supabase Storage object path
    call_start_ts = time.time()
    conversation_log: list = []  # collect chat_ctx turns for transcript

    if (is_voip or is_form_mode or is_advisor_room) and ListEgressRequest is not None:
        try:
            # Supabase S3-compatible endpoint: LiveKit uploads directly — no download/re-upload needed
            _supabase_url = os.getenv("SUPABASE_URL", "")
            _project_ref = _supabase_url.replace("https://", "").split(".")[0]
            _s3_endpoint = f"https://{_project_ref}.supabase.co/storage/v1/s3"
            _storage_path = f"{lead_id}/{ctx.room.name}.ogg" if lead_id else f"unknown/{ctx.room.name}.ogg"

            _s3_access_key = os.getenv("SUPABASE_S3_ACCESS_KEY_ID", "")
            _s3_secret = os.getenv("SUPABASE_S3_SECRET_ACCESS_KEY", "")

            from livekit.protocol.egress import S3Upload
            lk_api = LiveKitAPI(
                url=os.getenv("LIVEKIT_URL"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET"),
            )
            async with lk_api:
                if _s3_access_key and _s3_secret:
                    # Mode A: direct upload to Supabase S3 — no download/re-upload needed
                    browser_recording_path = _storage_path
                    s3_upload = S3Upload(
                        access_key=_s3_access_key,
                        secret=_s3_secret,
                        region="ap-south-1",
                        bucket="call-recordings",
                        endpoint=_s3_endpoint,
                        force_path_style=True,
                    )
                    # IMPORTANT: use singular `file=` (oneof field) not `file_outputs=[...]`.
                    # file_outputs is a legacy repeated field that doesn't set the `output`
                    # oneof — LiveKit server rejects it with 'missing or invalid field: output'.
                    egress_req = RoomCompositeEgressRequest(
                        room_name=ctx.room.name,
                        audio_only=True,
                        file=EncodedFileOutput(
                            file_type=EncodedFileType.OGG,
                            filepath=_storage_path,
                            s3=s3_upload,
                        ),
                    )
                    logger.info(f"[BrowserRec] Starting egress with direct S3 upload → call-recordings/{_storage_path}")
                else:
                    # Mode B: LiveKit stores egress internally; we download+upload in _save_browser_call
                    browser_recording_path = None
                    egress_req = RoomCompositeEgressRequest(
                        room_name=ctx.room.name,
                        audio_only=True,
                        file=EncodedFileOutput(
                            file_type=EncodedFileType.OGG,
                            filepath=f"browser-calls/{ctx.room.name}.ogg",
                        ),
                    )
                    logger.info(f"[BrowserRec] Starting egress (fallback mode — will download after call)")

                egress_info = await lk_api.egress.start_room_composite_egress(egress_req)
                egress_id = egress_info.egress_id
                logger.info(f"[BrowserRec] Egress started: {egress_id} for room {ctx.room.name}")
        except Exception as eg_start_err:
            logger.warning(f"[BrowserRec] Could not start Egress (will skip recording): {eg_start_err}")
            browser_recording_path = None

    # ── Transcript collector — attach to session events ────────────────────────
    # ConversationItemAddedEvent wraps the ChatMessage in .item — extract it.
    def _on_conversation_item(event):
        msg = getattr(event, "item", event)  # event.item is the ChatMessage
        conversation_log.append(msg)

    try:
        session.on("conversation_item_added", _on_conversation_item)
    except Exception:
        pass

    # ── Wait for disconnection ──────────────────────────────────────────────────
    from livekit.rtc import ConnectionState
    while ctx.room.connection_state != ConnectionState.CONN_DISCONNECTED:
        await asyncio.sleep(1)
    logger.info(f"Call ended for room: {ctx.room.name}")

    # ── Save browser call recording + transcript ────────────────────────────────
    if is_voip or is_form_mode or is_advisor_room:
        # Snapshot chat_ctx.items as fallback in case events were missed
        try:
            existing_ids = {id(m) for m in conversation_log}
            for msg in chat_ctx.items:
                if id(msg) not in existing_ids:
                    conversation_log.append(msg)
        except Exception:
            pass
        saved_lead_id = await _save_browser_call(
            ctx=ctx,
            lead_id=lead_id,
            room_name=ctx.room.name,
            egress_id=egress_id,
            recording_path=browser_recording_path,
            conversation_log=conversation_log,
            call_start_ts=call_start_ts,
        )
        if saved_lead_id:
            lead_id = saved_lead_id


    # ─── Post-Call Auto-Analysis & Supabase Update ───────────────────────────
    if lead_id:
        try:
            logger.info(f"Starting post-call analysis for lead: {lead_id}")
            messages = chat_ctx.messages()
            
            # Format chat transcript for the LLM
            transcript_lines = []
            for msg in messages:
                role = "Advisor" if msg.role == "assistant" else "Customer"
                content = agent._get_content_text(msg)
                if content:
                    transcript_lines.append(f"{role}: {content}")
            transcript = "\n".join(transcript_lines)

            if not transcript_lines:
                logger.warning("Call transcript is empty. Skipping post-call updates.")
                # Mark call as completed but no summary
                db = preload_lead.__globals__["_get_supabase"]()
                db.table("leads").update({
                    "call_status": "completed",
                    "call_completed_at": "now()"
                }).eq("id", lead_id).execute()
                return

            logger.info(f"Call Transcript:\n{transcript}")

            # Compact post-call analysis prompt (fewer tokens = faster, cheaper)
            prompt = (
                f"Analyze this insurance sales call. Return ONLY raw JSON, no markdown.\n\n"
                f"TRANSCRIPT:\n{transcript}\n\n"
                "Return: {\"summary\": \"2-sentence key points\", \"score\": 0-100, "
                "\"type\": \"hot|warm|cold\", \"rationale\": \"1 sentence why\"}\n"
                "hot=score>=80 (buying intent), warm=40-79 (interested), cold<40 (disinterested)"
            )

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
                        "Content-Type": "application/json",
                    },
                    json={
                        # Use a small fast model for scoring (migrated from deprecated llama-3.1-8b-instant)
                        "model": os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b"),
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 200,  # JSON output is tiny; cap prevents waste
                        "response_format": {"type": "json_object"},
                    }
                )
                response.raise_for_status()
                res_data = response.json()
                
            analysis_text = res_data["choices"][0]["message"]["content"]
            analysis = json.loads(analysis_text)
            logger.info(f"Groq post-call analysis: {analysis}")

            # Update Supabase lead details
            db = preload_lead.__globals__["_get_supabase"]()
            db.table("leads").update({
                "call_status": "completed",
                "call_completed_at": "now()",
                "call_summary": analysis.get("summary", ""),
                "ai_rank_score": analysis.get("score", 60),
                "profile_score": analysis.get("score", 60),
                "ai_rank_explanation": analysis.get("rationale", ""),
                "lead_type": analysis.get("type", "warm")
            }).eq("id", lead_id).execute()
            
            logger.info(f"Successfully updated lead status and ranking in Supabase for {lead_id}")

        except Exception as e:
            logger.error(f"Error performing post-call analysis for {lead_id}: {e}", exc_info=True)
            # Fallback update to mark completed
            try:
                db = preload_lead.__globals__["_get_supabase"]()
                db.table("leads").update({
                    "call_status": "completed",
                    "call_completed_at": "now()"
                }).eq("id", lead_id).execute()
            except Exception as db_err:
                logger.error(f"Failed to update database fallback status: {db_err}")


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # ── CRITICAL: num_idle_processes=1 ─────────────────────────────────
            # Default is 2 idle workers. Both receive the same room dispatch and
            # run entrypoint() simultaneously → two TTS streams play at once →
            # heard as garbled gibberish mid-call.
            # Setting to 1 ensures a single process handles each room.
            num_idle_processes=1,
        )
    )
