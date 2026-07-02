"""
tools/phrase_tokenizer.py — Phrase-level sentence tokenizer for Sarvam TTS.
──────────────────────────────────────────────────────────────────────────────
Why this exists:
  The default livekit SentenceTokenizer waits for a full sentence boundary
  (min_sentence_len=20 chars, hard . ! ?) before flushing text to TTS synthesis.
  For a 1–2 sentence Hindi/Hinglish reply this means the agent generates the
  entire reply before Sarvam TTS even starts synthesizing the first audio chunk.

  PhraseTokenizer flushes at SHORTER boundaries:
    - Hard sentence endings: .  !  ?  ।  ॥  (flush immediately)
    - Soft clause boundaries: ,  ;  :  (flush after MIN_WORDS_SOFT words)
    - Force flush:              after MAX_WORDS words even with no punctuation

  Effect on latency:
    BEFORE: LLM generates full sentence → TTS starts → audio
    AFTER:  LLM generates 5–8 words → TTS starts → rest of LLM generation
            overlaps with first TTS synthesis → user hears audio ~200–350ms sooner

Implementation:
  Fully implements the livekit SentenceTokenizer / SentenceStream ABC so it can
  be passed directly as the sentence_tokenizer= argument to tts.StreamAdapter.
"""

from __future__ import annotations

import uuid
from livekit.agents.tokenize.tokenizer import SentenceTokenizer, SentenceStream, TokenData


# ── Boundary sets ──────────────────────────────────────────────────────────────
_HARD_ENDS = frozenset(".!?।॥")    # Devanagari danda / double danda
_SOFT_ENDS = frozenset(",;:")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Streaming tokenizer ────────────────────────────────────────────────────────

class PhraseStream(SentenceStream):
    """
    Streaming implementation of SentenceStream.

    Accumulates text pushed via push_text() and emits TokenData events
    whenever a phrase boundary condition is met.

    Compatible with livekit.agents.tts.StreamAdapter — pass an instance of
    PhraseTokenizer as the sentence_tokenizer= argument.
    """

    def __init__(self, min_words_soft: int, max_words: int) -> None:
        super().__init__()                # initialises self._event_ch (aio.Chan)
        self._min_words_soft = min_words_soft
        self._max_words = max_words
        self._buf = ""
        self._word_count = 0
        self._segment_id = _short_id()

    # ── SentenceStream ABC ────────────────────────────────────────────────────

    def push_text(self, text: str) -> None:
        """Accept a new text chunk (typically one LLM token) and maybe emit a phrase."""
        self._check_not_closed()
        self._buf += text
        # Approximate word count: every space is a new word start
        self._word_count += text.count(" ")

        self._maybe_emit()

    def flush(self) -> None:
        """Force-emit whatever is currently buffered (called by StreamAdapter at end)."""
        self._check_not_closed()
        self._emit_buf()

    def end_input(self) -> None:
        """Signal that no more text is coming; flush remainder and close the channel."""
        self.flush()
        self._do_close()

    async def aclose(self) -> None:
        self._do_close()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _maybe_emit(self) -> None:
        """Check boundary conditions and emit if any are satisfied."""
        stripped = self._buf.rstrip()
        if not stripped:
            return
        last = stripped[-1]

        emit = (
            last in _HARD_ENDS                                          # sentence end
            or (last in _SOFT_ENDS and self._word_count >= self._min_words_soft)  # clause end
            or self._word_count >= self._max_words                      # force flush
        )

        if emit:
            self._emit_buf()

    def _emit_buf(self) -> None:
        """Emit the current buffer as a TokenData event and reset state."""
        phrase = self._buf.strip()
        if phrase:
            self._event_ch.send_nowait(
                TokenData(token=phrase, segment_id=self._segment_id)
            )
            self._segment_id = _short_id()
        self._buf = ""
        self._word_count = 0


# ── Tokenizer (factory) ────────────────────────────────────────────────────────

class PhraseTokenizer(SentenceTokenizer):
    """
    Drop-in replacement for tokenize.basic.SentenceTokenizer that flushes at
    phrase boundaries rather than full sentence boundaries.

    Usage:
        from tools.phrase_tokenizer import PhraseTokenizer
        wrapped_tts = tts.StreamAdapter(
            tts=self.session.tts,
            sentence_tokenizer=PhraseTokenizer(),
        )

    Args:
        min_words_soft: minimum word count before flushing at a soft boundary
                        (comma / semicolon / colon).  Default: 5.
        max_words:      force-flush after this many words regardless of punctuation.
                        Default: 12.
    """

    def __init__(
        self,
        *,
        min_words_soft: int = 5,
        max_words: int = 12,
    ) -> None:
        self._min_words_soft = min_words_soft
        self._max_words = max_words

    # ── SentenceTokenizer ABC ─────────────────────────────────────────────────

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        """Offline (batch) phrase splitting — used when the full text is available."""
        phrases: list[str] = []
        buf = ""
        words = 0

        for char in text:
            buf += char
            if char == " ":
                words += 1
            last = buf.rstrip()[-1] if buf.strip() else ""
            emit = (
                last in _HARD_ENDS
                or (last in _SOFT_ENDS and words >= self._min_words_soft)
                or words >= self._max_words
            )
            if emit and buf.strip():
                phrases.append(buf.strip())
                buf = ""
                words = 0

        if buf.strip():
            phrases.append(buf.strip())

        return phrases if phrases else [text]

    def stream(self, *, language: str | None = None) -> PhraseStream:
        """Create a streaming tokenizer instance for real-time token-by-token input."""
        return PhraseStream(
            min_words_soft=self._min_words_soft,
            max_words=self._max_words,
        )
