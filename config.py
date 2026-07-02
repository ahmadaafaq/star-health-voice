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

# ─── Plan name Devanagari Phonetics Map ──────────────────────────────────────────
# Used so that the Hindi TTS engine pronounces plan names correctly instead of spelling them out
PLAN_NAME_MAP = {
    # Full Names
    "Family Health Optima": "फैमिली हेल्थ ऑप्टिमा",
    "Arogya Sanjeevani": "आरोग्य संजीवनी",
    "Medi Classic": "मेडी क्लासिक",
    "Medi Classic Individual": "मेडी क्लासिक",
    "Star Health Assure": "स्टार हेल्थ एश्योर",
    "Star Premier": "स्टार हेल्थ प्रीमियर",
    "Star Health Premier": "स्टार हेल्थ प्रीमियर",
    "Young Star": "यंग स्टार",
    "Young Star Insurance": "यंग स्टार",
    "Super Star": "सुपर स्टार",
    "Star Comprehensive": "स्टार कॉम्प्रीहेंसिव",
    "Star Comprehensive Insurance Policy": "स्टार कॉम्प्रीहेंसिव",
    "Star Health": "स्टार हेल्थ",
    
    # Supabase leads.recommended_plan_id keys
    "family-health-optima": "फैमिली हेल्थ ऑप्टिमा",
    "arogya-sanjeevani": "आरोग्य संजीवनी",
    "medi-classic": "मेडी क्लासिक",
    "star-assure": "स्टार हेल्थ एश्योर",
    "star-premier": "स्टार हेल्थ प्रीमियर",
    "young-star": "यंग स्टार",
    "super-star": "सुपर स्टार",
    "star-comprehensive": "स्टार कॉम्प्रीहेंसिव"
}
