import twilio from 'twilio';
import { config } from './config.js';
import { Lead } from './supabase.js';

let twilioClient: twilio.Twilio;

try {
  twilioClient = twilio(config.twilio.accountSid, config.twilio.authToken);
} catch (error) {
  console.error("Failed to initialize Twilio client", error);
}

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

export function normalisePhone(raw: string): string {
  if (!raw) return "";
  const cleaned = raw.trim().replace(/^whatsapp:/i, "").trim();
  const digits = cleaned.replace(/\D/g, "");
  
  if (digits.startsWith("91") && digits.length === 12) {
    return `+${digits}`;
  }
  if (digits.length === 10) {
    return `+91${digits}`;
  }
  if (digits.startsWith("0") && digits.length === 11) {
    return `+91${digits.slice(1)}`;
  }
  return cleaned.startsWith("+") ? cleaned : (digits ? `+${digits}` : cleaned);
}

export async function initiateCall(lead: Lead, twimlUrl: string) {
  if (!twilioClient) {
    throw new Error("Twilio client is not initialized");
  }

  const normalisedTo = normalisePhone(lead.phone || '');
  if (!normalisedTo) {
    throw new Error(`Lead ${lead.id} has no valid phone number`);
  }

  try {
    // Determine designation based on gender
    let designation = '';
    if (lead.gender === 'male') designation = 'Sir';
    else if (lead.gender === 'female') designation = "Ma'am";
    
    // We encode the lead details into the TwiML URL so our webhook can generate dynamic TwiML
    const encodedName = encodeURIComponent(lead.name || '');
    const encodedDesignation = encodeURIComponent(designation);
    const policyName = getPolicyName(lead.policy || lead.recommended_plan_id || '');
    const encodedPolicy = encodeURIComponent(policyName);
    const encodedId = encodeURIComponent(lead.id);

    const fullTwimlUrl = `${twimlUrl}?leadId=${encodedId}&name=${encodedName}&designation=${encodedDesignation}&policy=${encodedPolicy}`;

    console.log(`Initiating call to ${normalisedTo} for lead ${lead.id}...`);

    const call = await twilioClient.calls.create({
      url: fullTwimlUrl,
      to: normalisedTo,
      from: config.twilio.phoneNumber,
    });

    console.log(`Call initiated successfully. Call SID: ${call.sid}`);
    return call;
  } catch (error) {
    console.error(`Error making call to ${normalisedTo}:`, error);
    throw error;
  }
}
