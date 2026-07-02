"""
scripts/build_policy_index.py
─────────────────────────────
Builds the local FAISS policy index for in-process search inside the voice agent.

Fetches policy chunk TEXT from Supabase, re-embeds locally using
all-MiniLM-L6-v2 (80MB, 384-dim, ~25ms encode) for fast runtime search.

Run this script whenever:
  - Policy documents are updated in Supabase
  - The embedding model is changed (required — dimensions must match)

    cd /path/to/star-health-voice-agent
    source venv/bin/activate
    python scripts/build_policy_index.py

Output files (written to policy_index/):
    chunks.json      — list of {text, policy} dicts (one per chunk)
    embeddings.npy   — float32 numpy array of shape (N, 384), L2-normalised

NOTE: Uses all-MiniLM-L6-v2 (384-dim). If you change the model here,
      you MUST also update tools/policy_rag.py to use the same model.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from supabase import create_client

# ── Load env ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent

RAG_ENV = ROOT.parent / "star-health-rag" / ".env"
if RAG_ENV.exists():
    load_dotenv(RAG_ENV)
    print(f"Loaded Supabase credentials from RAG repo: {RAG_ENV}")
else:
    load_dotenv(ROOT / ".env")
    print("RAG repo .env not found — using voice-agent .env credentials.")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY", "")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
).strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print(
        "ERROR: Could not find Supabase credentials.\n"
        "  Tried: SUPABASE_SERVICE_KEY and SUPABASE_SERVICE_ROLE_KEY\n"
        "  Make sure star-health-rag/.env exists with SUPABASE_URL and SUPABASE_SERVICE_KEY."
    )
    sys.exit(1)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Must match tools/policy_rag.py
OUTPUT_DIR = ROOT / "policy_index"
OUTPUT_DIR.mkdir(exist_ok=True)

CHUNKS_PATH     = OUTPUT_DIR / "chunks.json"
EMBEDDINGS_PATH = OUTPUT_DIR / "embeddings.npy"

# ── Fetch text chunks from Supabase (text only, no pre-computed embeddings) ───
print("Connecting to Supabase...")
db = create_client(SUPABASE_URL, SUPABASE_KEY)

print("Fetching policy chunks (policy_name, chunk_text)...")
res = db.table("policy_chunks").select("policy_name, chunk_text").execute()
rows = res.data or []
print(f"  → Fetched {len(rows)} rows from policy_chunks.")

# ── Build chunks metadata list ─────────────────────────────────────────────────
chunks_meta: list[dict] = []
texts: list[str] = []

for row in rows:
    text = (row.get("chunk_text") or "").strip()
    if not text:
        continue
    chunks_meta.append(
        {
            "text": text,
            "policy": row.get("policy_name", ""),
        }
    )
    texts.append(text)

print(f"  → {len(texts)} valid text chunks to embed.")

if not texts:
    print("ERROR: No valid text chunks found. Aborting.")
    sys.exit(1)

# ── Re-embed locally with all-MiniLM-L6-v2 ────────────────────────────────────
print(f"\nLoading SentenceTransformer model: {EMBEDDING_MODEL} ...")
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("ERROR: sentence-transformers not installed. Run: pip install sentence-transformers")
    sys.exit(1)

model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
print(f"  → Model loaded. Embedding {len(texts)} chunks (this may take a minute)...")

embeddings = model.encode(
    texts,
    normalize_embeddings=True,   # L2-normalise so IndexFlatIP == cosine similarity
    convert_to_numpy=True,
    show_progress_bar=True,
    batch_size=64,
).astype(np.float32)

print(f"  → Embedding matrix shape: {embeddings.shape}")

# ── Save ──────────────────────────────────────────────────────────────────────
with open(CHUNKS_PATH, "w", encoding="utf-8") as f:
    json.dump(chunks_meta, f, ensure_ascii=False, indent=2)

np.save(str(EMBEDDINGS_PATH), embeddings)

print("\n✅  Policy index built successfully!")
print(f"    chunks.json   : {CHUNKS_PATH}  ({len(chunks_meta)} chunks)")
print(f"    embeddings.npy: {EMBEDDINGS_PATH}  (shape={embeddings.shape}, dtype=float32)")
print(f"    Model used    : {EMBEDDING_MODEL}")
print("\nRun this script again whenever policy_chunks in Supabase are updated.")
