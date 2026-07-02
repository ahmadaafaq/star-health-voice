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
VAD_MIN_SILENCE_DURATION = 0.2   # seconds (200 ms)
VAD_ACTIVATION_THRESHOLD = 0.5

# ─── Star Health Plans (compact reference — injected into system prompt) ──────
STAR_HEALTH_PLANS_COMPACT = """
Plans reference (for quick answers, use search_policies tool for specific details):
1. Arogya Sanjeevani: affordable entry-level, ₹1–10L cover, 2yr PED wait, no room rent limit, AYUSH
2. Family Health Optima: family floater, restore benefit, bonus up to 100%, maternity, AYUSH
3. Medi Classic Individual: individual, ₹1.5–15L, organ donor, air ambulance, no sub-limits
4. Star Health Assure: comprehensive, ₹5–50L, chronic PED from day 1 (diabetes/hypertension), OPD
5. Star Premier: senior 50+ focus, no upper age limit, home care, AYUSH, ₹5–1Cr
6. Young Star: 18–40 age, wellness rewards, fitness discounts, ₹5–25L
7. Super Star: top-tier ₹15L–1Cr, unlimited restore, international cover, zero copay
8. Star Comprehensive: everything included maternity+PED+OPD+dental, ₹5–1Cr
"""

# ─── Agent persona ─────────────────────────────────────────────────────────────
AGENT_NAME = "Priya"
AGENT_ROLE = "Star Health Insurance digital advisor"
