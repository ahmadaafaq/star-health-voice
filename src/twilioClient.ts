import { exec } from 'child_process';
import { Lead } from './supabase.js';
import path from 'path';

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

export async function initiateCall(lead: Lead, twimlUrl: string): Promise<{ sid: string }> {
  const normalisedTo = normalisePhone(lead.phone || '');
  if (!normalisedTo) {
    throw new Error(`Lead ${lead.id} has no valid phone number`);
  }

  return new Promise((resolve, reject) => {
    // Run the Python LiveKit outbound SIP dispatch script
    const scriptPath = path.join(process.cwd(), 'make_call.py');
    const pythonPath = path.join(process.cwd(), 'venv', 'bin', 'python');
    
    const command = `"${pythonPath}" "${scriptPath}" --to "${normalisedTo}" --lead-id "${lead.id}"`;
    console.log(`Executing LiveKit outbound SIP call: ${command}`);

    exec(command, (error, stdout, stderr) => {
      if (error) {
        console.error(`Error initiating LiveKit SIP call: ${error.message}`);
        console.error(`Stderr: ${stderr}`);
        return reject(error);
      }
      
      console.log(`LiveKit dispatch stdout: ${stdout}`);
      
      // Generate a mock SID for UI/tracking
      const mockSid = `LK_${Math.random().toString(36).substring(2, 10).toUpperCase()}`;
      resolve({ sid: mockSid });
    });
  });
}
