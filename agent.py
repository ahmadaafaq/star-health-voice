"""
Star Health Insurance Voice Agent
──────────────────────────────────
Stack: LiveKit Cloud · Deepgram STT (Nova-2) · Groq LLM (llama-3.1-8b-instant) · Sarvam TTS (Anushka)
Memory: Supabase agent_memories table (persistent across calls)
RAG: In-process FAISS search (all-mpnet-base-v2, prewarmed) — HTTP fallback via star-health-rag

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

import config
from context_loader import preload_lead, build_system_prompt
from tools.memory import remember_detail, recall_detail, search_memories
from tools.phrase_tokenizer import PhraseTokenizer
from tools.policy_rag import search_policies, prewarm_policy_index
from tools.whatsapp import send_whatsapp_details

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
    prewarm_policy_index()


# ─── Agent class ─────────────────────────────────────────────────────────────

class StarHealthAgent(Agent):
    """
    Star Health Insurance voice advisor.
    - Greets the customer by name on call entry.
    - Has full lead profile + memories pre-loaded (no per-turn DB calls on common path).
    - Uses function tools for memory, policy RAG, and WhatsApp.
    """

    def __init__(self, *, chat_ctx: ChatContext, lead: dict, memories: list, is_voip: bool = False) -> None:
        super().__init__(
            chat_ctx=chat_ctx,
            instructions=build_system_prompt(lead, memories),
        )
        self._lead = lead
        self._memories = memories
        self._is_voip = is_voip

    async def on_enter(self) -> None:
        """Generate the opening greeting as soon as the agent joins the room."""
        name = self._lead.get("name", "")
        first_name = name.split()[0] if name else ""
        gender = self._lead.get("gender", "").strip().lower()
        salutation = "Sir" if gender == "male" else "Ma'am" if gender == "female" else "Sir or Ma'am"
        recommended_plan = self._lead.get("recommended_plan") or self._lead.get("recommendedPlan", "")

        if self._is_voip:
            greeting_instruction = (
                f"Greet the customer warmly since they initiated this browser call to speak with you. "
                f"Address them as {first_name or salutation}. "
                f"Say you are Priya from Star Health Insurance. "
                f"Say you are here to help them with their health insurance assessment "
                f"{'and answer any questions about the ' + recommended_plan + ' plan' if recommended_plan else ''}. "
                f"Ask how you can help them today. Keep it to 2 sentences maximum. "
                f"Never address them as bhaiya, didi, or other colloquial terms; only use Sir/Ma'am or their name."
            )
        else:
            greeting_instruction = (
                f"Greet the customer warmly on this outbound call. Their name is {name or 'unknown'}. "
                f"Address them as {first_name or salutation}. "
                f"Say you are Priya from Star Health Insurance. "
                f"Mention you are calling about their health insurance assessment "
                f"{'and their interest in the ' + recommended_plan + ' plan' if recommended_plan else ''}. "
                f"Ask if this is a good time to talk. Keep it to 2 sentences maximum. "
                f"Never address them as bhaiya, didi, or other colloquial terms; only use Sir/Ma'am or their name."
            )

        await self.session.generate_reply(instructions=greeting_instruction)

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: Any
    ):
        """
        Phrase-level TTS buffering for Sarvam Bulbul.

        Flushes text to TTS synthesis at:
          - Hard sentence boundaries  → . ! ? । ॥    (flush immediately)
          - Soft clause boundaries    → , ; :          (flush after 5+ words)
          - Force flush               → every 12 words (no punctuation fallback)

        This starts Sarvam synthesis on the first phrase while the LLM is still
        generating the remainder, overlapping LLM and TTS work and delivering
        first audio ~200–350ms sooner than sentence-level buffering.
        """
        from livekit.agents import tts
        from livekit.agents.utils import aio

        wrapped_tts = tts.StreamAdapter(
            tts=self.session.tts,
            sentence_tokenizer=PhraseTokenizer(
                min_words_soft=5,   # flush at comma/colon after 5+ words
                max_words=12,       # force flush if no punctuation in 12 words
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
        Prune conversation history before each LLM call to cap token usage.

        LiveKit accumulates every turn in chat_ctx. Without pruning, a 20-turn
        call sends ~2,400 tokens of history every turn — eating into the
        14,400 TPM free-tier limit and increasing TTFT.

        Strategy: keep last MAX_HISTORY_ITEMS items (14 ≈ 7 user+assistant pairs)
        + the system prompt is always preserved by ChatContext.truncate().
        This bounds context at ~1,100 tokens/turn regardless of call length.
        """
        MAX_HISTORY_ITEMS = 14   # 7 user+assistant pairs; system prompt added back automatically
        chat_ctx = chat_ctx.copy()  # don't mutate the live context
        if len(chat_ctx.items) > MAX_HISTORY_ITEMS:
            chat_ctx.truncate(max_items=MAX_HISTORY_ITEMS)
            logger.debug("History pruned to %d items", len(chat_ctx.items))
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)



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

    # ── WebRTC Inbound Call Fallback: Resolve lead_id from active room participants ──
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

                parts = identity.split("-")
                if len(parts) > 1 and parts[1] and parts[1] not in ("undefined", "null", "anonymous", ""):
                    lead_id = parts[1]
                    logger.info(f"Resolved lead_id from participant identity: {lead_id}")
                    break

    logger.info(f"Final resolved Lead ID for session: {lead_id}")

    # ── Pre-load lead + memories (once per call, no per-turn overhead) ─────────
    lead, memories = await preload_lead(lead_id)

    # ── Store context in userdata so tools can access it ───────────────────────
    ctx.proc.userdata["lead_id"] = lead_id
    ctx.proc.userdata["lead"] = lead

    # ── Build chat context (system prompt injected here) ───────────────────────
    chat_ctx = ChatContext()

    is_voip = ctx.room.name.startswith("browser-room-")
    agent = StarHealthAgent(chat_ctx=chat_ctx, lead=lead, memories=memories, is_voip=is_voip)

    # ── Configure the session ──────────────────────────────────────────────────
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(
        min_silence_duration=config.VAD_MIN_SILENCE_DURATION,
        activation_threshold=config.VAD_ACTIVATION_THRESHOLD,
    )

    session = AgentSession(
        stt=deepgram.STT(
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            smart_format=True,
            interim_results=True,
            endpointing_ms=200,
        ),
        llm=openai.LLM(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            model=config.GROQ_MODEL,
            temperature=config.GROQ_TEMPERATURE,
        ),
        tts=sarvam.TTS(
            model=config.SARVAM_MODEL,
            speaker=config.SARVAM_VOICE,
            target_language_code=config.SARVAM_LANGUAGE,
        ),
        vad=vad,
        tools=[search_policies, remember_detail, recall_detail, search_memories, send_whatsapp_details],
        userdata={"lead_id": lead_id, "lead": lead},
    )

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
            messages = list(chat_ctx.messages)
            
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
                        "model": config.GROQ_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
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
