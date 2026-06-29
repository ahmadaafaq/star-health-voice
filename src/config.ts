import dotenv from 'dotenv';

dotenv.config();

export const config = {
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL || '',
  supabaseKey: process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || '',
  twilio: {
    accountSid: process.env.TWILIO_ACCOUNT_SID || '',
    authToken: process.env.TWILIO_AUTH_TOKEN || '',
    phoneNumber: process.env.TWILIO_PHONE_NUMBER || ''
  },
  elevenlabs: {
    apiKey: process.env.ELEVENLABS_API_KEY || '',
    agentId: process.env.ELEVENLABS_AGENT_ID || ''
  },
  port: parseInt(process.env.PORT || '4000', 10)
};

const missingKeys = Object.entries(config).filter(([key, value]) => {
  if (typeof value === 'object') {
    return Object.values(value).some(v => !v);
  }
  return !value;
});

if (missingKeys.length > 0) {
  console.warn('Missing following config values:', missingKeys.map(([k]) => k).join(', '));
}
