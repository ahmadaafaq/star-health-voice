import { createClient } from '@supabase/supabase-js';
import { config } from './config.js';

if (!config.supabaseUrl || !config.supabaseKey) {
  console.error("Missing Supabase URL or Key");
}

export const supabase = createClient(config.supabaseUrl, config.supabaseKey);

export interface Lead {
  id: string;
  name: string;
  phone: string;
  email?: string;
  gender?: 'male' | 'female' | 'other';
  policy?: string;
  recommended_plan_id?: string;
  scheduled_call_at?: string;
  call_status?: 'pending' | 'dialing' | 'completed' | 'failed';
  age?: number;
  city?: string;
  // Onboarding profile fields
  members?: string[];
  budget?: string;
  pre_existing_diseases?: string[];
  diabetes?: boolean;
  parents_included?: boolean;
  employer_insurance?: boolean;
  pregnancy_plan?: boolean;
  preferred_hospital?: string;
  // AI recommendation & scoring
  ai_rank_score?: number;
  ai_rank_explanation?: string;
  lead_type?: string;
}
