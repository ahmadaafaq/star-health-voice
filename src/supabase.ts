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
}
