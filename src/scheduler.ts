import cron from 'node-cron';
import { supabase, Lead } from './supabase.js';
import { initiateCall } from './twilioClient.js';
import { config } from './config.js';

// Change this to your public URL where the webhook will be hosted
// In production, this should be an environment variable like process.env.APP_URL
const APP_URL = process.env.APP_URL || `http://localhost:${config.port}`;
const TWIML_URL = `${APP_URL}/api/voice/twiml`;

export function startScheduler() {
  console.log("Starting cron scheduler for outbound calls...");

  // Run every 1 minute
  cron.schedule('* * * * *', async () => {
    console.log(`[${new Date().toISOString()}] Checking for scheduled calls...`);
    try {
      // Find leads where call_scheduled = true or scheduled_call_at is in the past/now
      // For this example, we assume `call_status` = 'pending' and `scheduled_call_at` <= NOW()
      const now = new Date().toISOString();
      
      const { data: leads, error } = await supabase
        .from('leads')
        .select('*')
        .in('call_status', ['pending', 'scheduled'])
        .lte('scheduled_call_at', now)
        .limit(10); // batch size

      if (error) {
        console.error("Error fetching leads from Supabase:", error);
        return;
      }

      if (!leads || leads.length === 0) {
        console.log("No pending calls scheduled.");
        return;
      }

      console.log(`Found ${leads.length} pending calls. Initiating...`);

      for (const lead of leads as Lead[]) {
        try {
          // 1. Mark as dialing so we don't pick it up again
          await supabase
            .from('leads')
            .update({ call_status: 'dialing' })
            .eq('id', lead.id);

          // 2. Trigger Twilio Call
          await initiateCall(lead, TWIML_URL);

          // Optionally, update status to 'dialed' or store the Call SID
        } catch (err) {
          console.error(`Failed to process lead ${lead.id}:`, err);
          // Revert status on failure
          await supabase
            .from('leads')
            .update({ call_status: 'failed' })
            .eq('id', lead.id);
        }
      }

    } catch (err) {
      console.error("Unexpected error in cron job:", err);
    }
  });
}
