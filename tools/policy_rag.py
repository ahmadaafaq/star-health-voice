"""
tools/policy_rag.py — On-demand policy information retrieval.

Calls the star-health-rag service's /api/search endpoint to answer very specific
policy questions using pgvector similarity search over policy_chunks in Supabase.

This tool is only invoked when the customer asks a detailed question that is NOT
covered by the compact plans reference already in the system prompt. This keeps
the common-path latency at zero while still supporting deep questions.
"""

import logging
import os

import httpx
from livekit.agents import function_tool, RunContext
from dotenv import load_dotenv

load_dotenv(".env")
logger = logging.getLogger("star-health-agent.policy_rag")

RAG_API_URL = os.getenv("RAG_API_URL", "http://localhost:8005")


@function_tool()
async def search_policies(context: RunContext, query: str) -> str:
    """
    Search Star Health policy documents for a specific customer question.
    Call this tool for detailed questions about: waiting periods, sub-limits,
    exclusions, room rent caps, claim process, specific benefit details, co-pay rules,
    or any policy specifics not covered by your general knowledge.
    Do NOT call this for general plan comparisons — use your built-in knowledge for those.

    Args:
        query: The customer's question or the specific detail you need to look up.
    """
    logger.info(f"Policy RAG search: {query}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{RAG_API_URL}/api/search",
                json={"query": query, "limit": 3},
            )
            response.raise_for_status()
            data = response.json()

        chunks = data.get("results", [])
        if not chunks:
            logger.warning(f"No policy chunks found for query: {query}")
            return "I don't have specific details on that, but I can connect you with our product team."

        # Combine top chunks into a compact answer
        combined = " ".join(c.get("content", "") for c in chunks[:3])
        # Trim to keep LLM context lean (500 chars max from RAG)
        return combined[:500] if len(combined) > 500 else combined

    except httpx.TimeoutException:
        logger.warning(f"RAG service timeout for query: {query}")
        return "I'm checking our policy documents — let me answer from what I know."
    except Exception as e:
        logger.error(f"Error in search_policies: {e}")
        return "I'll need to confirm that detail with our team. Can I have someone follow up with you on WhatsApp?"
