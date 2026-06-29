import express from 'express';
import { config } from './config.js';
import { startScheduler } from './scheduler.js';
import webhookRoutes from './routes/webhook.js';

const app = express();

app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

import axios from 'axios';

// Routes
app.use('/api/voice', webhookRoutes);

// Proxy WhatsApp Webhook to the Python RAG Backend
app.post('/webhook/whatsapp', async (req, res) => {
  try {
    const params = new URLSearchParams();
    for (const key in req.body) {
      params.append(key, req.body[key]);
    }
    
    const RAG_API_URL = process.env.RAG_API_URL || 'http://localhost:8005';
    const response = await axios.post(`${RAG_API_URL}/webhook/whatsapp`, params.toString(), {
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded'
      }
    });
    
    res.set('Content-Type', 'application/xml');
    res.send(response.data);
  } catch (error) {
    console.error("Error proxying WhatsApp webhook to Python RAG:", error);
    res.status(500).send('<Response></Response>');
  }
});

// Health check endpoint
app.get('/health', (req, res) => {
  res.json({ status: 'ok', service: 'star-health-voice-agent' });
});

app.listen(config.port, () => {
  console.log(`Star Health Voice Agent server running on port ${config.port}`);
  
  // Start the background cron scheduler
  startScheduler();
});
