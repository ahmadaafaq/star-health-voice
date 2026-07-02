"""
Star Health Voice Agent — Central Configuration
All model names, voice settings, and API defaults live here.
"""

# ─── LLM (Groq) ───────────────────────────────────────────────────────────────
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TEMPERATURE = 0.5            # lower = shorter, more predictable voice replies
GROQ_MAX_TOKENS = 120             # 1-2 sentences ≈ 40-80 tokens; cap prevents runaway generation
                                  # and directly reduces TTFT + protects TPM quota

# ─── STT (Deepgram) ───────────────────────────────────────────────────────────
DEEPGRAM_STT_MODEL = "nova-2-general"
DEEPGRAM_STT_LANGUAGE = "hi"     # Hindi & Hinglish support (note: 'hi-en' is invalid in Deepgram and causes 400 errors)

# ─── TTS (Sarvam) ─────────────────────────────────────────────────────────────
SARVAM_MODEL = "bulbul:v2"
SARVAM_VOICE = "manisha"           # Female voice compatible with bulbul:v2 (anushka, manisha, vidya are the female options)
SARVAM_LANGUAGE = "hi-IN"         # Synthesizes Hindi/Hinglish speech

# ─── VAD (Silero) ─────────────────────────────────────────────────────────────
# Lower min_silence_duration = faster response but may interrupt the user
VAD_MIN_SILENCE_DURATION = 0.25   # seconds (250 ms) — minimum allowed by LiveKit's TurnDetector
VAD_ACTIVATION_THRESHOLD = 0.5

# ─── Star Health Plans (compact reference — injected into system prompt) ──────
STAR_HEALTH_PLANS_COMPACT = """
Plans summary (call search_policies for details):
- Arogya Sanjeevani: Entry-level, ₹1-10L.
- Family Health Optima: Floater, restore benefit.
- Medi Classic: Individual, ₹1.5-15L.
- Star Health Assure: Comprehensive, ₹5-50L.
- Star Premier: 50+ Senior focus, ₹10L-1Cr.
- Young Star: 18-40 focus, ₹5-25L.
- Super Star: Top-tier, ₹15L-1Cr.
- Star Comprehensive: Premium with OPD, maternity, ₹5L-1Cr.
"""

# ─── Agent persona ─────────────────────────────────────────────────────────────
AGENT_NAME = "Priya"
AGENT_ROLE = "Star Health Insurance digital advisor"
