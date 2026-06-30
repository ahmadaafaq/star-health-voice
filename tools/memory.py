"""
tools/memory.py — Agentic conversation memory backed by Supabase.

Uses a simple UPSERT model: each (lead_id, memory_type) is a named slot.
Writing a new value replaces the old one. This gives the agent a persistent
short-term memory that survives across calls with the same customer.

Pattern adapted from livekit-examples/supabase-hacker-starter.
"""

import logging
import os
from typing import Optional

from supabase import create_client, Client
from livekit.agents import function_tool, RunContext
from dotenv import load_dotenv

load_dotenv(".env")
logger = logging.getLogger("star-health-agent.memory")

_supabase: Optional[Client] = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        _supabase = create_client(url, key)
    return _supabase


@function_tool()
async def remember_detail(context: RunContext, label: str, value: str) -> str:
    """
    Save a fact about the customer for future calls.
    Use a short label like 'preferred_name', 'preferred_language', 'budget_preference',
    'family_status', 'concern_about_cost', 'interested_in_plan', etc.
    Each label is a unique slot; saving a new value replaces the old one.

    Args:
        label: Short snake_case label for the fact (e.g., 'preferred_name')
        value: The fact to remember (e.g., 'Mr. Ahmad')
    """
    lead_id = context.userdata.get("lead_id")
    if not lead_id:
        return "Memory not saved — no customer identified."

    try:
        db = _get_supabase()
        db.table("agent_memories").upsert(
            {
                "lead_id": lead_id,
                "memory_type": label.strip().lower().replace(" ", "_"),
                "content": value.strip(),
            },
            on_conflict="lead_id,memory_type"
        ).execute()
        logger.info(f"Memory saved: lead={lead_id}, {label}={value}")
        return f"Got it, I've noted that."
    except Exception as e:
        logger.error(f"Error saving memory: {e}")
        return "I noted that for our conversation."


@function_tool()
async def recall_detail(context: RunContext, label: str) -> str:
    """
    Retrieve a previously saved fact about the customer by its exact label.
    Use this only when you know the exact label. For a fuzzy search use search_memories.

    Args:
        label: The exact label to retrieve (e.g., 'preferred_name')
    """
    lead_id = context.userdata.get("lead_id")
    if not lead_id:
        return "No customer context available."

    try:
        db = _get_supabase()
        res = (
            db.table("agent_memories")
            .select("content")
            .eq("lead_id", lead_id)
            .eq("memory_type", label.strip().lower())
            .maybe_single()
            .execute()
        )
        if res and res.data:
            return res.data["content"]
        return f"I don't have anything noted for '{label}'."
    except Exception as e:
        logger.error(f"Error recalling memory: {e}")
        return "I couldn't recall that detail right now."


@function_tool()
async def search_memories(context: RunContext, query: str) -> str:
    """
    Search past conversation notes about this customer using full-text search.
    Use when you don't know the exact label but need to recall something.

    Args:
        query: Natural language description of what you're looking for (e.g., 'what did customer say about budget')
    """
    lead_id = context.userdata.get("lead_id")
    if not lead_id:
        return "No customer context available."

    try:
        db = _get_supabase()
        # Full-text search across memory content
        res = (
            db.table("agent_memories")
            .select("memory_type, content")
            .eq("lead_id", lead_id)
            .ilike("content", f"%{query}%")
            .limit(5)
            .execute()
        )
        if res and res.data:
            lines = [f"{m['memory_type']}: {m['content']}" for m in res.data]
            return "\n".join(lines)
        return "Nothing relevant found in my notes."
    except Exception as e:
        logger.error(f"Error searching memories: {e}")
        return "I couldn't search my notes right now."
