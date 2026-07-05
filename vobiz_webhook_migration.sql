-- Run this in Supabase SQL Editor
-- Adds columns to store Vobiz recording and transcription data per lead

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS vobiz_call_uuid       text,          -- Vobiz CallUUID for deduplication
  ADD COLUMN IF NOT EXISTS call_transcription    text,          -- Full AI transcription text from Vobiz
  ADD COLUMN IF NOT EXISTS call_duration_seconds integer;       -- Call duration in seconds from Vobiz

-- Index for fast lookup when webhook arrives (match lead by phone)
CREATE INDEX IF NOT EXISTS leads_phone_idx ON leads (phone);
