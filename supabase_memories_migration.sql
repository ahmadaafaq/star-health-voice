-- ═══════════════════════════════════════════════════════════════════════════
-- Star Health Voice Agent — Supabase Migration
-- Creates the agent_memories table for persistent conversation memory.
-- Run this in the Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1. Create the agent_memories table
CREATE TABLE IF NOT EXISTS agent_memories (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    memory_type text NOT NULL,          -- e.g., 'preferred_name', 'budget_concern'
    content     text NOT NULL,          -- the actual memory content
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE(lead_id, memory_type)        -- one slot per label per lead
);

-- 2. Auto-update updated_at on upsert
CREATE OR REPLACE FUNCTION update_agent_memories_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_memories_updated_at ON agent_memories;
CREATE TRIGGER trg_agent_memories_updated_at
    BEFORE UPDATE ON agent_memories
    FOR EACH ROW
    EXECUTE FUNCTION update_agent_memories_updated_at();

-- 3. Index for fast per-lead lookups
CREATE INDEX IF NOT EXISTS idx_agent_memories_lead_id
    ON agent_memories(lead_id);

-- 4. Enable Row Level Security (optional but recommended)
ALTER TABLE agent_memories ENABLE ROW LEVEL SECURITY;

-- Allow service role (used by agent) full access
CREATE POLICY "Service role full access on agent_memories"
    ON agent_memories
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- 5. Verify
SELECT
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'agent_memories'
ORDER BY ordinal_position;
