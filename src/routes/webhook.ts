import express from 'express';
import { supabase } from '../supabase.js';
import { config } from '../config.js';
import axios from 'axios';
import crypto from 'crypto';
import { initiateCall } from '../twilioClient.js';

const router = express.Router();

// Local helper to convert policy IDs to human-readable names
export function getPolicyName(policyId: string): string {
  const plans: Record<string, string> = {
    "family-health-optima": "Family Health Optima",
    "arogya-sanjeevani": "Arogya Sanjeevani",
    "medi-classic": "Medi Classic (Individual)",
    "star-assure": "Star Health Assure",
    "star-premier": "Star Health Premier",
    "young-star": "Young Star Insurance",
    "super-star": "Super Star"
  };
  return plans[policyId] || policyId;
}

// Rules-based analyzer to parse conversation summary and compute lead priority ranking
export function analyzeCallSummary(summary: string) {
  let score = 60; // default score
  let type = "warm";
  let explanation = "Assessed from conversation summary.";

  if (!summary) {
    return { score, type, explanation };
  }

  const lower = summary.toLowerCase();

  if (lower.includes("not interested") || lower.includes("reject") || lower.includes("no need") || lower.includes("refused") || lower.includes("hang up") || lower.includes("hung up")) {
    score = 25;
    type = "cold";
    explanation = "Customer expressed no interest, hung up, or rejected the policy during the call.";
  } else if (lower.includes("highly interested") || lower.includes("buy") || lower.includes("purchas") || lower.includes("ready") || lower.includes("wants to proceed") || lower.includes("consent") || lower.includes("confirm")) {
    score = 85;
    type = "hot";
    explanation = "Customer showed high interest and requested details to buy or proceed with the recommended plan.";
  } else if (lower.includes("schedule") || lower.includes("call back") || lower.includes("thinking") || lower.includes("callback") || lower.includes("compare") || lower.includes("discuss")) {
    score = 65;
    type = "warm";
    explanation = "Customer showed moderate interest and requested a follow-up or callback to discuss details.";
  } else {
    explanation = "Call successfully completed. Moderate intent detected from discussion details.";
  }

  return { score, type, explanation };
}

// Twilio webhook to handle answered outbound calls
router.post('/twiml', express.urlencoded({ extended: true }), async (req, res) => {
  try {
    console.log(`Received /twiml webhook from Twilio for CallSid: ${req.body.CallSid}`);
    // These were passed as query parameters in our initiateCall function
    const leadId = req.query.leadId as string;
    const name = req.query.name as string || '';
    const designation = req.query.designation as string || 'Sir / Ma\'am';
    const rawPolicy = req.query.policy as string || '';
    const policy = getPolicyName(rawPolicy);

    // Request secure TwiML from ElevenLabs using the register-call endpoint
    const response = await axios.post(
      'https://api.elevenlabs.io/v1/convai/twilio/register-call',
      {
        agent_id: config.elevenlabs.agentId,
        from_number: config.twilio.phoneNumber, // We might want to pass the actual From if available
        to_number: req.body.To || req.query.to || '',
        conversation_initiation_client_data: {
          dynamic_variables: {
            name: name,
            customer_name: name,
            customerName: name,
            user_name: name,
            userName: name,
            designation: designation,
            policy: policy,
            policyName: policy,
            policy_name: policy,
            recommended_plan: policy,
            recommended_policy: policy,
            recommended_plan_name: policy
          }
        }
      },
      {
        headers: {
          'xi-api-key': config.elevenlabs.apiKey,
          'Content-Type': 'application/json'
        }
      }
    );

    res.set('Content-Type', 'text/xml');
    res.send(response.data);
  } catch (error) {
    console.error('Error generating TwiML:', error);
    res.status(500).send('Error generating TwiML');
  }
});

// Test endpoint to manually trigger a call for Arjun
router.post('/test-call', express.json(), async (req, res) => {
  try {
    const testLead: any = {
      id: "test-arjun-123",
      name: "Arjun",
      phone: req.body.phone || config.twilio.phoneNumber, // fallback to something or require it
      gender: "male",
      policy: req.body.policy || "Family Health Optima"
    };

    const APP_URL = process.env.APP_URL || `http://localhost:${config.port}`;
    const TWIML_URL = `${APP_URL}/api/voice/twiml`;

    const call = await initiateCall(testLead, TWIML_URL);
    res.json({ success: true, callSid: call.sid });
  } catch (error: any) {
    console.error("Test call error:", error);
    res.status(500).json({ error: error.message });
  }
});

// Endpoint to trigger outbound call for a specific lead ID in Supabase
router.post('/trigger-outbound', express.json(), async (req, res) => {
  let leadId = req.body.leadId;
  try {
    if (!leadId) {
      return res.status(400).json({ error: "leadId is required" });
    }

    console.log(`Triggering manual outbound call request for lead ID: ${leadId}`);

    // Fetch the lead from Supabase
    const { data: lead, error } = await supabase
      .from('leads')
      .select('*')
      .eq('id', leadId)
      .single();

    if (error || !lead) {
      console.error(`Lead not found or error fetching lead:`, error);
      return res.status(404).json({ error: "Lead not found in Supabase" });
    }

    // 1. Mark as dialing
    await supabase
      .from('leads')
      .update({ call_status: 'dialing' })
      .eq('id', leadId);

    const APP_URL = process.env.APP_URL || `http://localhost:${config.port}`;
    const TWIML_URL = `${APP_URL}/api/voice/twiml`;

    // 2. Trigger the call
    const call = await initiateCall(lead as any, TWIML_URL);

    res.json({ success: true, callSid: call.sid });
  } catch (error: any) {
    console.error("Manual outbound call trigger error:", error);
    // Mark back to failed
    if (leadId) {
      await supabase
        .from('leads')
        .update({ call_status: 'failed' })
        .eq('id', leadId);
    }
    res.status(500).json({ error: error.message });
  }
});

// Post-call webhook to receive data from ElevenLabs after the conversation ends
router.post('/post-call', express.json({
  limit: '50mb',
  verify: (req: any, res, buf) => {
    req.rawBody = buf;
  }
}), async (req, res) => {
  try {
    const signatureHeader = req.headers['elevenlabs-signature'] as string;
    const webhookSecret = process.env.ELEVENLABS_WEBHOOK_SECRET;

    if (webhookSecret && signatureHeader) {
      const parts = signatureHeader.split(',');
      const tPart = parts.find(p => p.startsWith('t='));
      const v0Part = parts.find(p => p.startsWith('v0='));

      if (tPart && v0Part) {
        const timestamp = tPart.substring(2);
        const signature = v0Part.substring(3);

        const rawBodyStr = (req as any).rawBody ? (req as any).rawBody.toString('utf8') : JSON.stringify(req.body);
        const signedPayload = `${timestamp}.${rawBodyStr}`;

        const computedSignature = crypto
          .createHmac('sha256', webhookSecret)
          .update(signedPayload)
          .digest('hex');

        if (computedSignature !== signature) {
          console.error("Invalid ElevenLabs Webhook signature matching failed!");
          return res.status(401).send('Invalid signature');
        }
        console.log("ElevenLabs Webhook signature verified successfully.");
      } else {
        console.warn("ElevenLabs Webhook signature header missing expected format.");
        return res.status(400).send('Invalid signature format');
      }
    }

    const data = req.body;
    console.log("Received post-call webhook from ElevenLabs:", JSON.stringify(data, null, 2));

    const conversationId = data.conversation_id;
    const phoneNumber = data.call_info?.user_phone_number;
    
    // 1. Link conversation back to the lead in Supabase
    let lead: any = null;

    if (conversationId) {
      const { data: leadData } = await supabase
        .from('leads')
        .select('*')
        .eq('conversation_id', conversationId)
        .limit(1);
      if (leadData && leadData.length > 0) {
        lead = leadData[0];
      }
    }

    if (!lead && phoneNumber) {
      const { data: leadData } = await supabase
        .from('leads')
        .select('*')
        .eq('phone', phoneNumber)
        .limit(1);
      if (leadData && leadData.length > 0) {
        lead = leadData[0];
      }
    }

    if (!lead) {
      console.warn(`Could not associate conversation ${conversationId} or phone ${phoneNumber} with any lead.`);
      return res.status(200).send('Lead not found, logging skipped.');
    }

    console.log(`Associated webhook conversation ${conversationId} with lead ${lead.id} (${lead.name})`);

    // Elevenlabs data collection webhook structure
    const dataCollection = data.data_collection || {};
    const interestLevel = dataCollection.interest_level?.value || 'unknown';
    const primaryNeed = dataCollection.primary_need?.value || '';
    const rawRecommendedPlan = dataCollection.recommended_plan?.value || '';
    const recommendedPlan = getPolicyName(rawRecommendedPlan);
    const followUpScheduled = dataCollection.follow_up_scheduled?.value === true;
    const budgetRange = dataCollection.budget_range?.value || '';
    const summary = data.analysis?.summary || '';

    // 2. Download Call Recording from ElevenLabs
    let recordingUrl = '';
    if (conversationId && config.elevenlabs.apiKey) {
      try {
        console.log(`Downloading audio recording for conversation ${conversationId} from ElevenLabs...`);
        const audioResponse = await axios.get(
          `https://api.elevenlabs.io/v1/convai/conversations/${conversationId}/audio`,
          {
            headers: {
              'xi-api-key': config.elevenlabs.apiKey
            },
            responseType: 'arraybuffer'
          }
        );
        
        const audioBuffer = Buffer.from(audioResponse.data);
        console.log(`Successfully downloaded audio (${audioBuffer.length} bytes). Uploading to Cloudinary...`);
        
        // Cloudinary upload using standard API
        const cloudinaryCloudName = process.env.CLOUDINARY_CLOUD_NAME;
        const cloudinaryApiKey = process.env.CLOUDINARY_API_KEY;
        const cloudinarySecret = process.env.CLOUDINARY_API_SECRET;
        
        if (cloudinaryCloudName && cloudinaryApiKey && cloudinarySecret) {
          const timestamp = Math.round(Date.now() / 1000);
          const signatureStr = `timestamp=${timestamp}${cloudinarySecret}`;
          const signature = crypto.createHash('sha1').update(signatureStr).digest('hex');
          
          const uploadResponse = await axios.post(
            `https://api.cloudinary.com/v1_1/${cloudinaryCloudName}/video/upload`,
            {
              file: `data:audio/mpeg;base64,${audioBuffer.toString('base64')}`,
              api_key: cloudinaryApiKey,
              timestamp: timestamp,
              signature: signature
            }
          );
          
          recordingUrl = uploadResponse.data.secure_url;
          console.log(`Successfully uploaded call recording to Cloudinary. URL: ${recordingUrl}`);
        } else {
          console.warn("Cloudinary credentials missing, skipping audio recording upload.");
        }
      } catch (err: any) {
        console.error("Error downloading or uploading call recording:", err.message);
      }
    }

    // 3. Compute priority ranking and score based on conversation summary
    const analysisResult = analyzeCallSummary(summary);
    console.log(`Call summary re-ranked lead ${lead.id}. Score: ${analysisResult.score}, Category: ${analysisResult.type}`);

    // 4. Update the DB lead details
    const updatePayload: any = {
      call_status: 'completed',
      interest_level: interestLevel,
      primary_need: primaryNeed,
      recommended_plan_id: lead.recommended_plan_id || rawRecommendedPlan,
      policy: recommendedPlan || lead.policy,
      follow_up_scheduled: followUpScheduled,
      budget_range: budgetRange,
      call_completed_at: new Date().toISOString(),
      call_summary: summary,
      ai_rank_score: analysisResult.score,
      profile_score: analysisResult.score,
      ai_rank_explanation: analysisResult.explanation,
      lead_type: analysisResult.type
    };

    if (recordingUrl) {
      updatePayload.call_recording_url = recordingUrl;
    }

    const { error: dbError } = await supabase
      .from('leads')
      .update(updatePayload)
      .eq('id', lead.id);

    if (dbError) {
      console.error(`Error updating Supabase database for lead ${lead.id}:`, dbError);
      return res.status(500).send('Database update error');
    }

    console.log(`Updated lead ${lead.id} successfully post-call.`);
    res.status(200).send('OK');
  } catch (error) {
    console.error('Error processing post-call webhook:', error);
    res.status(500).send('Error processing post-call data');
  }
});

export default router;
