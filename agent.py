"""
Star Health Insurance Voice Agent
──────────────────────────────────
Stack: LiveKit Cloud · Deepgram STT (Nova-2) · Groq LLM (llama-3.3-70b) · Sarvam TTS (Anushka)
Memory: Supabase agent_memories table (persistent across calls)
RAG: star-health-rag /api/search via search_policies tool (on-demand only)

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
from tools.policy_rag import search_policies
from tools.whatsapp import send_whatsapp_details

load_dotenv(".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("star-health-agent")


# ─── Pre-warm: load Silero VAD model once per worker process ──────────────────

def prewarm(proc: JobProcess):
    """Load heavy models once when the worker starts, not per-call."""
    logger.info("Prewarming Silero VAD model...")
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=config.VAD_MIN_SILENCE_DURATION,
        activation_threshold=config.VAD_ACTIVATION_THRESHOLD,
    )
    logger.info("Silero VAD loaded.")


# ─── Agent class ─────────────────────────────────────────────────────────────

class StarHealthAgent(Agent):
    """
    Star Health Insurance voice advisor.
    - Greets the customer by name on call entry.
    - Has full lead profile + memories pre-loaded (no per-turn DB calls on common path).
    - Uses function tools for memory, policy RAG, and WhatsApp.
    """

    def __init__(self, *, chat_ctx: ChatContext, lead: dict, memories: list) -> None:
        super().__init__(
            chat_ctx=chat_ctx,
            instructions=build_system_prompt(lead, memories),
        )
        self._lead = lead
        self._memories = memories

    async def on_enter(self) -> None:
        """Generate the opening greeting as soon as the agent joins the room."""
        name = self._lead.get("name", "")
        first_name = name.split()[0] if name else ""
        recommended_plan = self._lead.get("recommended_plan") or self._lead.get("recommendedPlan", "")

        greeting_instruction = (
            f"Greet the customer warmly. Their name is {name or 'unknown'}. "
            f"Address them as {first_name or 'Sir or Ma am'}. "
            f"Say you are Priya from Star Health Insurance. "
            f"Mention you are calling about their health insurance assessment "
            f"{'and their interest in the ' + recommended_plan + ' plan' if recommended_plan else ''}. "
            "Ask if this is a good time to talk. Keep it to 2 sentences maximum."
        )

        await self.session.generate_reply(instructions=greeting_instruction)


# ─── Entrypoint — called for every new call/room ─────────────────────────────

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
    logger.info(f"Lead ID from metadata: {lead_id}")

    # ── Pre-load lead + memories (once per call, no per-turn overhead) ─────────
    lead, memories = await preload_lead(lead_id)

    # ── Store context in userdata so tools can access it ───────────────────────
    ctx.proc.userdata["lead_id"] = lead_id
    ctx.proc.userdata["lead"] = lead

    # ── Build chat context (system prompt injected here) ───────────────────────
    chat_ctx = ChatContext()

    # ── Create the agent ───────────────────────────────────────────────────────
    agent = StarHealthAgent(chat_ctx=chat_ctx, lead=lead, memories=memories)

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
            interim_results=False,   # final transcripts only — reduces noise & latency
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
    )

    # ── Register function tools on the session ─────────────────────────────────
    session.register_tool(search_policies)
    session.register_tool(remember_detail)
    session.register_tool(recall_detail)
    session.register_tool(search_memories)
    session.register_tool(send_whatsapp_details)

    # ── Start the session ──────────────────────────────────────────────────────
    await session.start(ctx.room, agent=agent)
    logger.info("Agent session started. Waiting for call to end...")

    await session.wait_for_disconnection()
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

            # Call Groq to generate summary & score
            prompt = f"""You are an expert health insurance lead conversion analyst.
Analyze the following transcript of a phone conversation between Priya (our Star Health Advisor) and the customer:

TRANSCRIPT:
{transcript}

Tasks:
1. Summarize the key discussion points, customer requirements, and concerns (under 2-3 sentences).
2. Evaluate their likelihood to purchase a plan on a scale of 0 to 100.
3. Determine lead category:
   - 'hot' (Score >= 80): High intent, e.g., wants to buy, requested follow-up to buy, scheduling payment.
   - 'warm' (Score 40-79): Moderate intent, e.g., asked questions, interested but needs time, scheduled a general callback.
   - 'cold' (Score < 40): Low intent, e.g., hung up early, not interested, rejected the policy.
4. Provide a brief explanation of the score.

Return ONLY a raw JSON object (no markdown, no surrounding text):
{{
  "summary": "...",
  "score": 85,
  "type": "hot",
  "rationale": "Explanation based on the conversation."
}}"""

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
