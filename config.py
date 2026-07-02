"""
Star Health Voice Agent — Central Configuration
All model names, voice settings, and API defaults live here.
"""

# ─── LLM (Groq) ───────────────────────────────────────────────────────────────
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_TEMPERATURE = 0.5            # lower = shorter, more predictable voice replies
GROQ_MAX_TOKENS = 150             # 1-2 sentences ≈ 40-80 tokens; cap prevents runaway generation
                                  # and directly reduces TTFT + protects 14.4K TPM quota

# ─── STT (Deepgram) ───────────────────────────────────────────────────────────
DEEPGRAM_STT_MODEL = "nova-2-general"
DEEPGRAM_STT_LANGUAGE = "hi"       # Hindi & Hinglish support

# ─── TTS (Sarvam) ─────────────────────────────────────────────────────────────
SARVAM_MODEL = "bulbul:v2"
SARVAM_VOICE = "anushka"          # Anushka supports both hi-IN and en-IN
SARVAM_LANGUAGE = "hi-IN"         # Synthesizes Hindi/Hinglish speech

# ─── VAD (Silero) ─────────────────────────────────────────────────────────────
# Lower min_silence_duration = faster response but may interrupt the user
VAD_MIN_SILENCE_DURATION = 0.25   # seconds (250 ms) — minimum allowed by LiveKit's TurnDetector
VAD_ACTIVATION_THRESHOLD = 0.5

# ─── Star Health Plans (compact reference — injected into system prompt) ──────
STAR_HEALTH_PLANS_COMPACT = """
Plans summary (call search_policies for details):
1. Arogya Sanjeevani: Entry-level, ₹1-10L.
2. Family Health Optima: Family floater, restore benefit.
3. Medi Classic Individual: Individual, ₹1.5-15L.
4. Star Health Assure: Comprehensive, ₹5-50L, day-1 chronic PED.
5. Star Premier: Senior focus, ₹5-1Cr.
6. Young Star: Age 18-40 focus, ₹5-25L.
7. Super Star: Top-tier, ₹15L-1Cr.
8. Star Comprehensive: Premium all-inclusive, ₹5-1Cr.
"""

# ─── Agent persona ─────────────────────────────────────────────────────────────
AGENT_NAME = "Priya"
AGENT_ROLE = "Star Health Insurance digital advisor"
