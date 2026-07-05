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
import certifi

# Fix macOS SSL issues — must be before all other imports
os.environ["SSL_CERT_FILE"] = certifi.where()

import asyncio
import json
import logging
import httpx
from typing import Any, AsyncIterable

from dotenv import load_dotenv
from livekit import agents
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
from livekit.plugins import deepgram, openai, silero, sarvam
try:
    from livekit.plugins import elevenlabs
except ImportError:
    elevenlabs = None

import config
from context_loader import preload_lead, build_system_prompt, build_form_assistant_prompt
from tools.memory import remember_detail, recall_detail, search_memories
from tools.phrase_tokenizer import PhraseTokenizer
from tools.policy_rag import search_policies, prewarm_policy_index
from tools.whatsapp import send_whatsapp_details
from tools.form_control import update_form_field, advance_form_step, submit_form, go_to_form_step, set_room_step, get_current_form_state

load_dotenv(".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("star-health-agent")


# ─── Pre-warm: load Silero VAD model once per worker process ──────────────────

def prewarm(proc: JobProcess):
    """Load heavy models ONCE when the worker process starts — never per-call."""
    # 1. Silero VAD (used for end-of-speech detection)
    logger.info("Prewarming Silero VAD model...")
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=config.VAD_MIN_SILENCE_DURATION,
        activation_threshold=config.VAD_ACTIVATION_THRESHOLD,
    )
    logger.info("Silero VAD loaded.")

    # 2. Policy search: SentenceTransformer + FAISS index (in-process, zero HTTP)
    # Run in a background thread so it doesn't block the LiveKit process startup (10s timeout)
    import threading
    threading.Thread(target=prewarm_policy_index, daemon=True).start()


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
        if is_advisor_room:
            instructions = build_system_prompt(lead, memories)
        elif is_form_mode:
            instructions = build_form_assistant_prompt()
        else:
            instructions = build_system_prompt(lead, memories)
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
        advisor_instructions = build_system_prompt(self._lead, self._memories)

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
        self.chat_ctx.add_message(
            role="system",
            content=(
                f"[ROLE SWITCH — PERMANENT]: The form is complete. You are NO LONGER a form assistant. "
                f"You are now a Star Health Insurance Policy Advisor. "
                f"The customer is {lead_name}. Their recommended plan is '{plan_name}' (ID: {plan_id}). "
                f"Monthly premium: ₹{monthly_premium}. "
                f"NEVER ask any form-filling questions. "
                f"Warmly congratulate them in Hinglish, tell them their plan is ready, "
                f"and offer to explain plan benefits, compare plans, or send details on WhatsApp. "
                f"Use search_policies() to answer any plan questions silently in the background.]"
            )
        )
        logger.info(f"Advisor mode activated for plan: {plan_id} ({plan_name}), premium: {monthly_premium}")

    async def on_enter(self) -> None:
        """Generate the opening as soon as the agent joins the room."""

        # ── Advisor-room reconnection mode ────────────────────────────────────
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
            plan_hi = config.PLAN_NAME_MAP.get(recommended_plan, recommended_plan) or "your recommended plan"
            await self.session.generate_reply(
                instructions=(
                    f"You are a Star Health Insurance Policy Advisor. The customer is {first_name or 'the customer'}. "
                    f"Their recommended plan is '{plan_hi}'. "
                    f"Greet them warmly in Hinglish (1-2 sentences), tell them their plan recommendation is ready, "
                    f"and offer to explain plan benefits, compare with other plans, or send details on WhatsApp. "
                    f"Do NOT ask any form-filling questions. You are NOT a form assistant."
                )
            )
            return

        # ── Form-filling assistant mode ───────────────────────────────────────
        if self._is_form_mode:
            initial_step = getattr(self, "_initial_step", 1)
            if initial_step <= 1:
                # Fresh start — greet and ask the opening question
                await self.session.generate_reply(
                    instructions=(
                        "Start immediately. Say exactly 1 short Hindi sentence: "
                        "'हाय, मैं प्रिया हूँ स्टार हेल्थ से।' "
                        "Then immediately ask the first question: "
                        "'आप किसको कवर करना चाहेंगे — सिर्फ आप, या फैमिली भी कवर करनी है?' "
                        "No other text. No intro. No welcome speech."
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

        # ── Standard advisor mode (VoIP / outbound call) ─────────────────────
        name = self._lead.get("name", "")
        first_name_raw = name.split()[0] if name else ""
        first_name = config.COMMON_NAMES_MAP.get(first_name_raw, first_name_raw)
        gender = self._lead.get("gender", "").strip().lower()
        salutation = "Sir" if gender == "male" else "Ma'am" if gender == "female" else "Sir or Ma'am"
        recommended_plan = (
            self._lead.get("recommended_plan_id")
            or self._lead.get("recommended_plan")
            or self._lead.get("recommendedPlan")
            or ""
        )
        plan_hi = config.PLAN_NAME_MAP.get(recommended_plan, recommended_plan) or "a plan tailored for you"

        recommendation_reason = (
            self._lead.get("recommendation_reason")
            or self._lead.get("why_this_plan")
            or ""
        )
        reason_snippet = ""
        if recommendation_reason:
            words = recommendation_reason.split()
            reason_snippet = " ".join(words[:12]) + ("…" if len(words) > 12 else "")

        is_scheduled = (self._lead.get("call_status") == "scheduled") or bool(self._lead.get("scheduled_call_at"))
        question_suffix = "क्या ये बात करने का अच्छा समय है?" if not is_scheduled else "Can we proceed?"

        if self._is_voip:
            greeting_instruction = (
                f"Greet the customer in warm Hinglish (Hindi words in Devanagari + English terms). "
                f"Address the customer as '{address_name}' (use Devanagari script in text, e.g. नमन जी). "
                f"Say you are Priya (प्रिया) from Star Health (स्टार हेल्थ) Insurance. "
                f"Immediately mention their recommended plan: '{plan_hi}'. "
                f"{('Briefly say why: ' + reason_snippet + '. ') if reason_snippet else ''}"
                f"Then ask: 'क्या आप इसके बारे में जानना चाहेंगे?' (or similar). "
                f"Keep it to exactly 2 natural spoken sentences. "
                f"No markdown, no lists. Never use bhaiya, didi"
            )
        else:
            greeting_instruction = (
                f"Start the call by speaking exactly this Hinglish greeting, using Devanagari script for Hindi: "
                f"'{first_name or 'नमन'} जी, आपने हमारी website पर insurance search किया था and हमने आपको {plan_hi} plan recommend किया था। {question_suffix}' "
                f"Do not add any other introductions (do not say 'main Priya hoon Star Health se' or similar). "
                f"Output exactly these two sentences."
            )

        await self.session.generate_reply(instructions=greeting_instruction)


    async def tts_node(
        self, text: AsyncIterable[str], model_settings: Any
    ):
        """
        Phrase-level TTS buffering.

        For Sarvam: flushes text to TTS at phrase boundaries to overlap LLM and TTS.
        For ElevenLabs: passthrough — ElevenLabs plugin handles its own streaming
        natively and double-wrapping adds buffering latency.
        """
        # ElevenLabs streams natively — skip the PhraseTokenizer wrapper
        if config.TTS_PROVIDER == "elevenlabs":
            async for frame in Agent.default.tts_node(self, text, model_settings):
                yield frame
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
                async for ev in stream:
                    yield ev.frame
            finally:
                await aio.cancel_and_wait(forward_task)
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
        # Form filling needs very short context (3 turns); advisor needs slightly more (4 turns)
        MAX_TURNS = 3 if self._is_form_mode else 4

        from livekit.agents.llm import ChatMessage
        all_items = [m for m in chat_ctx.items if isinstance(m, ChatMessage)]
        
        # Keep only the primary persona system message or role switch prompt.
        # Temporary system notifications are kept only if they are extremely recent (e.g. within the last 3 items)
        # to ensure they are processed exactly once and then discarded.
        system_msgs = []
        for idx, m in enumerate(all_items):
            if m.role == "system":
                if "You are Priya" in m.content or "ROLE SWITCH" in m.content:
                    system_msgs.append(m)
                elif "[System Notification:" in m.content:
                    # Keep only if it is very recent (e.g., last 3 messages of chat context)
                    if len(all_items) - idx <= 3:
                        system_msgs.append(m)

        conv_msgs = [m for m in all_items if m.role != "system"]

        # Group conversation messages into complete turns starting with a user message
        turns = []
        current_turn = []
        for msg in conv_msgs:
            if msg.role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)

        if len(turns) > MAX_TURNS:
            pruned_conv = []
            for turn in turns[-MAX_TURNS:]:
                pruned_conv.extend(turn)
            
            pruned_ctx = ChatContext()
            pruned_ctx.items.extend(system_msgs)
            pruned_ctx.items.extend(pruned_conv)
            chat_ctx = pruned_ctx
            logger.info("History pruned to %d complete turns (%d messages)", MAX_TURNS, len(pruned_conv))
        else:
            # Even if turns are within MAX_TURNS, we still want to prune old system notifications
            pruned_ctx = ChatContext()
            pruned_ctx.items.extend(system_msgs)
            pruned_ctx.items.extend(conv_msgs)
            chat_ctx = pruned_ctx

        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)

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
        import re
        _MARKDOWN_RE = re.compile(
            r"\*{1,2}|#{1,6}\s?|\d+\.\s|^[-•]\s", re.MULTILINE
        )

        async def _clean(stream):
            # Banned sequences (in lowercase, cleaned of punctuation)
            BANNED_SEQUENCES = [
                ["send", "whatsapp", "details"],
                ["search", "policies"],
                ["remember", "detail"],
                ["policy", "name"],
                ["customer", "source"],
                ["value", "database"],
                ["details", "label"]
            ]
            
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
                
                # Numeric variations with commas
                "1,099": "एक हजार निन्यानवे",
                "1,199": "एक हजार एक सौ निन्यानवे",
                "1,499": "एक हजार चार सौ निन्यानवे",
                "1,899": "एक हजार आठ सौ निन्यानवे",
                "1,999": "एक हजार नौ सौ निन्यानवे",
                "2,299": "दो हजार दो सौ निन्यानवे",
                "2,499": "दो हजार चार सौ निन्यानवे",

                # Lakh / Crore / Rupees
                "lakh": "लाख",
                "lakhs": "लाख",
                "crore": "करोड़",
                "crores": "करोड़",
                "rupee": "रुपये",
                "rupees": "रुपये",
                
                # Age limits and general numbers
                "18": "अठारह",
                "40": "चालीस",
                "50": "पचास",
                "50+": "पचास से ज्यादा",
                "9": "नौ",
                
                # Single digits
                "5": "पांच",
                "10": "दस",
                "15": "पंद्रह",
                "25": "पच्चीस",
                "1": "एक",
                "2": "दो",
            }

            buffer = ""
            pending_words = []

            def get_clean_token(w: str) -> str:
                return re.sub(r"[^\w]", "", w).lower()

            def apply_replacements(word: str) -> str:
                # Strip currency symbols first
                word_clean = word.replace("₹", "").replace("$", "")
                match = re.match(r"^([^\w]*)(.*?)([^\w]*)$", word_clean)
                if not match:
                    return word_clean
                lead_punc, core, tail_punc = match.groups()
                core_lower = core.lower()
                if core_lower in WORD_REPLACEMENTS:
                    return lead_punc + WORD_REPLACEMENTS[core_lower] + tail_punc
                return word_clean

            async def flush_pending(n: int = 0):
                while len(pending_words) > n:
                    w = pending_words.pop(0)
                    yield w + " "

            async for chunk in stream:
                if not isinstance(chunk, str):
                    async for w in flush_pending(0):
                        yield w
                    yield chunk
                    continue

                buffer += chunk

                # Hard-wipe any function name that appears anywhere in the streamed buffer
                buffer = FUNC_NAME_RE.sub("", buffer)
                
                # Check for word-splitting character (whitespace)
                if not any(c in chunk for c in (" ", "\t", "\n")):
                    continue
                
                # Clean basic markdown and backticks
                cleaned_buffer = _MARKDOWN_RE.sub("", buffer)
                cleaned_buffer = cleaned_buffer.replace("`", "")
                
                words = cleaned_buffer.split()
                if not words:
                    continue
                
                # Check if the last word is incomplete (does not end with whitespace)
                last_incomplete = not buffer[-1].isspace()
                
                words_to_process = words[:-1] if (last_incomplete and len(words) > 1) else words
                
                if last_incomplete and len(words) > 1:
                    buffer = words[-1] + (" " if buffer.endswith(" ") else "")
                else:
                    buffer = ""

                for w in words_to_process:
                    w_replaced = apply_replacements(w)
                    chk = re.sub(r"[^\w_]", "", w_replaced).lower()
                    if "_" in chk or chk in BANNED_WORDS:
                        continue
                        
                    pending_words.append(w_replaced)
                    
                    # 1. Check for complete sequence match at the end
                    matched = False
                    for seq in BANNED_SEQUENCES:
                        n_seq = len(seq)
                        if len(pending_words) >= n_seq:
                            last_tokens = [get_clean_token(x) for x in pending_words[-n_seq:]]
                            if last_tokens == seq:
                                pending_words = pending_words[:-n_seq]
                                matched = True
                                break
                    
                    if matched:
                        continue
                        
                    # 2. Check if the pending list forms a prefix of any banned sequence
                    while pending_words:
                        is_prefix = False
                        for seq in BANNED_SEQUENCES:
                            check_len = min(len(pending_words), len(seq))
                            pending_clean_sub = [get_clean_token(x) for x in pending_words[:check_len]]
                            if seq[:check_len] == pending_clean_sub:
                                is_prefix = True
                                break
                        
                        if is_prefix:
                            break
                        else:
                            yield pending_words.pop(0) + " "

            # Flush remaining buffer at the end
            async for w in flush_pending(0):
                yield w
            if buffer.strip():
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

    logger.info(f"Final resolved Lead ID for session: {lead_id}")

    # ── Pre-load lead + memories (once per call, no per-turn overhead) ─────────
    lead, memories = await preload_lead(lead_id)

    # ── Detect room type ────────────────────────────────────────────────────────
    is_voip = ctx.room.name.startswith("browser-room-")
    is_form_mode = ctx.room.name.startswith("form-room-")
    is_advisor_room = ctx.room.name.startswith("advisor-room-")

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
    )

    if config.TTS_PROVIDER == "elevenlabs":
        if elevenlabs is None:
            raise ImportError("livekit-plugins-elevenlabs is not installed or failed to import. Run pip install livekit-plugins-elevenlabs.")
        logger.info(f"Initializing ElevenLabs TTS: voice={config.ELEVENLABS_VOICE_ID} model={config.ELEVENLABS_MODEL}")
        tts_engine = elevenlabs.TTS(
            api_key=config.ELEVENLABS_API_KEY,
            voice_id=config.ELEVENLABS_VOICE_ID,
            model=config.ELEVENLABS_MODEL,
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
            smart_format=True,
            interim_results=True,
            endpointing_ms=100,     # VAD handles primary end-of-speech at 200ms;
                                    # 100ms here avoids double-stacking to 400ms total
        ),
        llm=openai.LLM(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            model=config.GROQ_MODEL,
            temperature=config.GROQ_TEMPERATURE,
            max_completion_tokens=config.GROQ_MAX_TOKENS,
        ),
        tts=tts_engine,
        vad=vad,
        tools=agent_tools,
        userdata={"lead_id": lead_id, "lead": lead, "room": ctx.room},
        max_tool_steps=5,  # Cap consecutive tool calls to 5 per turn to allow multiple updates + transition
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
                logger.info(f"Received initial_step_context. Agent opened mid-form on Step: {step}")
                # Update the agent's known initial step
                agent._initial_step = step
                set_room_step(ctx.room.name, step)
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
                        f"[System Notification: The customer started the voice assistant mid-form on Step {step}: {step_label}. "
                        f"Do NOT introduce yourself with the Step 1 greeting. "
                        f"Skip any steps already completed. "
                        f"Greet briefly and immediately ask the first question for Step {step}.]"
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
                
                # 2. Update the room state fields map so get_current_form_state is accurate
                from tools.form_control import _get_state
                state = _get_state(ctx)
                if "fields" not in state:
                    state["fields"] = {}
                state["fields"][field] = value
                
                # 3. Inject a system message so the LLM does not call update_form_field for this field again
                chat_ctx.add_message(
                    role="system",
                    content=(
                        f"[System Notification: The customer manually updated form field '{field}' to '{value}' on their screen. "
                        f"Your internal record for this field has been updated. "
                        f"Acknowledge the update if relevant, and do NOT call update_form_field for '{field}' with the old value.]"
                    )
                )
        except Exception as e:
            logger.error(f"Error handling data packet: {e}")

    # ── Start the session ──────────────────────────────────────────────────────
    await session.start(agent=agent, room=ctx.room)
    logger.info("Agent session started. Waiting for call to end...")

    # ── Wait for disconnection ──────────────────────────────────────────────────
    from livekit.rtc import ConnectionState
    while ctx.room.connection_state != ConnectionState.CONN_DISCONNECTED:
        await asyncio.sleep(1)
    logger.info(f"Call ended for room: {ctx.room.name}")

    # ─── Post-Call Auto-Analysis & Supabase Update ───────────────────────────
    if lead_id:
        try:
            logger.info(f"Starting post-call analysis for lead: {lead_id}")
            messages = chat_ctx.messages()
            
            # Format chat transcript for the LLM
            transcript_lines = []
            for msg in messages:
                role = "Advisor" if msg.role == "assistant" else "Customer"
                content = msg.content or ""
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
                        # Use a small fast model for scoring — no need for 70B here
                        "model": "llama-3.1-8b-instant",
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
        )
    )
