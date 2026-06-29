-- Run this script in the Supabase SQL Editor to add the required voice agent columns

ALTER TABLE leads 
  -- Information to personalize the agent greeting
  ADD COLUMN IF NOT EXISTS gender text CHECK (gender IN ('male', 'female', 'other')),
  ADD COLUMN IF NOT EXISTS policy text,

  -- Call scheduling and status tracking
  ADD COLUMN IF NOT EXISTS scheduled_call_at timestamptz,
  ADD COLUMN IF NOT EXISTS call_status text DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS call_completed_at timestamptz,
  
  -- ElevenLabs post-call analysis data
  ADD COLUMN IF NOT EXISTS interest_level text,
  ADD COLUMN IF NOT EXISTS primary_need text,
  ADD COLUMN IF NOT EXISTS follow_up_scheduled boolean,
  ADD COLUMN IF NOT EXISTS budget_range text,
  
  -- WhatsApp opt-in tracking
  ADD COLUMN IF NOT EXISTS whatsapp_consent boolean DEFAULT false,
  
  -- VOIP call tracking and recording columns
  ADD COLUMN IF NOT EXISTS conversation_id text,
  ADD COLUMN IF NOT EXISTS call_summary text,
  ADD COLUMN IF NOT EXISTS call_recording_url text,
  
  -- Campaign grouping column
  ADD COLUMN IF NOT EXISTS campaign_name text DEFAULT 'Manual Import';
