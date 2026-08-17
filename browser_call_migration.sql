-- ─── Browser Call Recording Migration ────────────────────────────────────────
-- Run this in Supabase SQL Editor (Dashboard > SQL Editor > New query)
-- Adds columns to leads table for browser (WebRTC) call data
-- separate from Vobiz telephony columns.

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS browser_call_recording_url  text,         -- Supabase Storage signed URL path
  ADD COLUMN IF NOT EXISTS browser_call_transcription  text,         -- Full conversation transcript
  ADD COLUMN IF NOT EXISTS browser_call_duration_secs  integer,      -- Call duration in seconds
  ADD COLUMN IF NOT EXISTS browser_call_at             timestamptz;  -- When the call happened

-- Private storage bucket for call audio files
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'call-recordings',
  'call-recordings',
  false,
  52428800,
  ARRAY['audio/ogg', 'audio/mpeg', 'audio/wav', 'audio/mp4', 'video/mp4']
)
ON CONFLICT (id) DO NOTHING;

-- Service-role only write access
DROP POLICY IF EXISTS "Service upload call-recordings" ON storage.objects;
CREATE POLICY "Service upload call-recordings" ON storage.objects
  FOR INSERT TO authenticated
  WITH CHECK (bucket_id = 'call-recordings');

-- Service-role can read (for signed URL generation)
DROP POLICY IF EXISTS "Service read call-recordings" ON storage.objects;
CREATE POLICY "Service read call-recordings" ON storage.objects
  FOR SELECT TO authenticated
  USING (bucket_id = 'call-recordings');
