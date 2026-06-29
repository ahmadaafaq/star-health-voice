import axios from 'axios';
import { config } from './config.js';

const apiKey = config.elevenlabs.apiKey;

async function run() {
  try {
    console.log("Listing workspace webhooks...");
    const res = await axios.get(
      'https://api.elevenlabs.io/v1/workspace/webhooks',
      {
        headers: {
          'xi-api-key': apiKey
        }
      }
    );
    console.log("Webhooks:", JSON.stringify(res.data, null, 2));
  } catch (err: any) {
    console.error("Error listing webhooks:", err.response?.data || err.message);
  }
}

run();
