"""
scripts/build_policy_index.py
─────────────────────────────
One-time script to pull policy chunk embeddings from Supabase and save them
as a local numpy index for in-process FAISS search inside the voice agent.

Run this script whenever policy documents are updated in Supabase:

    cd /path/to/star-health-voice-agent
    python scripts/build_policy_index.py

Output files (written to policy_index/):
    chunks.json      — list of {text, policy} dicts (one per chunk)
    embeddings.npy   — float32 numpy array of shape (N, 768), L2-normalised

The existing embeddings stored in Supabase were generated with
all-mpnet-base-v2 (768-dim). We re-use them as-is — no re-embedding needed.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from supabase import create_client

# ── Load env ──────────────────────────────────────────────────────────────────
# policy_chunks lives in the same Supabase project as the RAG service.
# The RAG repo uses SUPABASE_SERVICE_KEY; the voice-agent uses SUPABASE_SERVICE_ROLE_KEY.
# We try the RAG repo's .env first (it has guaranteed access to policy_chunks),
# then fall back to the voice-agent's own .env.
ROOT = Path(__file__).parent.parent

# Resolve sibling RAG repo path
RAG_ENV = ROOT.parent / "star-health-rag" / ".env"
if RAG_ENV.exists():
    load_dotenv(RAG_ENV)
    print(f"Loaded Supabase credentials from RAG repo: {RAG_ENV}")
else:
    load_dotenv(ROOT / ".env")
    print("RAG repo .env not found — using voice-agent .env credentials.")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
# RAG repo key: SUPABASE_SERVICE_KEY
# Voice-agent key: SUPABASE_SERVICE_ROLE_KEY
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

OUTPUT_DIR = ROOT / "policy_index"
OUTPUT_DIR.mkdir(exist_ok=True)

CHUNKS_PATH = OUTPUT_DIR / "chunks.json"
EMBEDDINGS_PATH = OUTPUT_DIR / "embeddings.npy"

# ── Fetch ─────────────────────────────────────────────────────────────────────
print("Connecting to Supabase...")
db = create_client(SUPABASE_URL, SUPABASE_KEY)

print("Fetching policy chunks (policy_name, chunk_text, embedding)...")
res = db.table("policy_chunks").select("policy_name, chunk_text, embedding").execute()
rows = res.data or []
print(f"  → Fetched {len(rows)} rows from policy_chunks.")

# ── Parse embeddings ──────────────────────────────────────────────────────────
chunks_meta: list[dict] = []
raw_embeddings: list[list[float]] = []

skipped = 0
for row in rows:
    emb_val = row.get("embedding")
    if not emb_val:
        skipped += 1
        continue

    # pgvector returns embeddings in several possible string formats
    if isinstance(emb_val, str):
        cleaned = emb_val.strip()
        try:
            if cleaned.startswith("[") and cleaned.endswith("]"):
                emb_list = json.loads(cleaned)
            elif cleaned.startswith("{") and cleaned.endswith("}"):
                emb_list = [float(x) for x in cleaned[1:-1].split(",")]
            else:
                emb_list = [float(x) for x in cleaned.strip("[]{} ").split(",")]
        except Exception as e:
            print(f"  WARN: could not parse embedding for row — skipping. ({e})")
            skipped += 1
            continue
    elif isinstance(emb_val, list):
        emb_list = [float(x) for x in emb_val]
    else:
        skipped += 1
        continue

    chunks_meta.append(
        {
            "text": row.get("chunk_text", ""),
            "policy": row.get("policy_name", ""),
        }
    )
    raw_embeddings.append(emb_list)

print(f"  → Parsed {len(raw_embeddings)} valid chunks  |  Skipped: {skipped}")

if not raw_embeddings:
    print("ERROR: No valid embeddings found. Aborting.")
    sys.exit(1)

# ── Normalise (L2) so FAISS IndexFlatIP gives cosine similarity ───────────────
emb_array = np.array(raw_embeddings, dtype=np.float32)
norms = np.linalg.norm(emb_array, axis=1, keepdims=True)
norms[norms == 0] = 1.0          # avoid division by zero for zero vectors
emb_normalised = emb_array / norms

print(f"  → Embedding matrix shape: {emb_normalised.shape}")

# ── Save ──────────────────────────────────────────────────────────────────────
with open(CHUNKS_PATH, "w", encoding="utf-8") as f:
    json.dump(chunks_meta, f, ensure_ascii=False, indent=2)

np.save(str(EMBEDDINGS_PATH), emb_normalised)

print("\n✅  Policy index built successfully!")
print(f"    chunks.json   : {CHUNKS_PATH}  ({len(chunks_meta)} chunks)")
print(f"    embeddings.npy: {EMBEDDINGS_PATH}  (shape={emb_normalised.shape}, dtype=float32)")
print("\nRun this script again whenever policy_chunks in Supabase are updated.")
