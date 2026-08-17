import os

# ─── LLM (Groq) ───────────────────────────────────────────────────────────────
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")  # Migrated from deprecated llama-3.3-70b-versatile
GROQ_TEMPERATURE = 0.4            # lower = shorter, more predictable voice replies
GROQ_MAX_TOKENS = 150             # 1-2 sentences; generates richer responses so allow slightly more headroom


# ─── STT (Deepgram) ───────────────────────────────────────────────────────────
DEEPGRAM_STT_MODEL = "nova-2-general"
DEEPGRAM_STT_LANGUAGE = "hi"     # Hindi & Hinglish support (note: 'hi-en' is invalid in Deepgram and causes 400 errors)

TTS_PROVIDER = "elevenlabs"            # Options: "sarvam" or "elevenlabs"

# ElevenLabs Settings
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "6kpMXeRmTQXHAKa2goju")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")  # Recommended multilingual low-latency model for LiveKit WebSocket streaming
                                                                         # (eleven_v3 is REST-only and returns 403 on WebSocket multi-stream-input)

# Expressive Voice Settings
# Lower stability (0.35) unlocks emotional pitch dynamics & inflection; high stability (>0.7) forces flat monotone speech.
ELEVENLABS_STABILITY = float(os.getenv("ELEVENLABS_STABILITY", "0.35"))
ELEVENLABS_SIMILARITY = float(os.getenv("ELEVENLABS_SIMILARITY", "0.75"))
ELEVENLABS_STYLE = float(os.getenv("ELEVENLABS_STYLE", "0.20"))  # Lowered from 0.45: reduces ElevenLabs server processing time by ~100ms


# Sarvam Settings
SARVAM_MODEL = "bulbul:v2"
SARVAM_VOICE = "manisha"           # Female voice compatible with bulbul:v2 (anushka, manisha, vidya are the female options)
SARVAM_LANGUAGE = "hi-IN"         # Synthesizes Hindi/Hinglish speech

# ─── VAD (Silero) ─────────────────────────────────────────────────────────────
# Lower min_silence_duration = faster response but may interrupt the user
VAD_MIN_SILENCE_DURATION = 0.30   # seconds — 8kHz silero halves lag vs 0.25 original; 0.30 is a safe buffer
VAD_ACTIVATION_THRESHOLD = 0.5

# ─── Star Health Plans (compact reference — injected into system prompt) ──────
STAR_HEALTH_PLANS_COMPACT = """
Plans summary:
- Arogya Sanjeevani: Entry-level standard policy, starting at 799 Rupees per month, 5 Lakh to 2 Crore Rupees coverage.
- Family Health Optima: Floater with restoration benefit, starting at 1,199 Rupees per month, 5 Lakh to 25 Lakh Rupees coverage.
- Medi Classic: Individual classic health cover, starting at 899 Rupees per month, 5 Lakh to 15 Lakh Rupees coverage.
- Star Health Assure: Comprehensive floater (covers up to 9 members), starting at 1,499 Rupees per month, 5 Lakh to 2 Crore Rupees coverage.
- Star Premier: Senior citizen policy (50+ age, no pre-policy tests), starting at 1,899 Rupees per month, 10 Lakh to 1 Crore Rupees coverage.
- Young Star: Tailored for young adults (18-40 age, unlimited restoration), starting at 699 Rupees per month, 5 Lakh to 1 Crore Rupees coverage.
- Super Star: Flagship top-tier premium coverage, starting at 2,299 Rupees per month, 5 Lakh to 5 Crore Rupees coverage.
- Star Comprehensive: Premium policy with OPD, maternity & global cover, starting at 1,099 Rupees per month, 5 Lakh to 1 Crore Rupees coverage.
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

# ─── Plan Starting Prices (source of truth — injected into system prompt) ────────────────────
PLAN_PRICE_MAP = {
    "young-star": "699",
    "family-health-optima": "1,199",
    "arogya-sanjeevani": "799",
    "medi-classic": "899",
    "star-assure": "1,499",
    "star-premier": "1,899",
    "super-star": "2,299",
    "star-comprehensive": "1,099",
}

# ─── Common Names Devanagari Phonetics Map ──────────────────────────────────────
# Translates English names to Devanagari in python before passing to LLM to prevent TTS pronunciation distortion
COMMON_NAMES_MAP = {
    "Aditya": "आदित्य",
    "aditya": "आदित्य",
    "Naman": "नमन",
    "naman": "नमन",
    "Arman": "अरमान",
    "arman": "अरमान",
    "Arjun": "अर्जुन",
    "arjun": "अर्जुन",
    "Priya": "प्रिया",
    "priya": "प्रिया",
    "Shreya": "श्रेया",
    "shreya": "श्रेया",
    "Manisha": "मनीषा",
    "manisha": "मनीषा",
    "Amit": "अमित",
    "amit": "अमित",
    "Vidya": "विद्या",
    "vidya": "विद्या",
    "Anushka": "अनुष्का",
    "anushka": "अनुष्का"
}
