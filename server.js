// ==========================================
// Tank Monitor — Unified Backend Server
// ==========================================
// Bridges MQTT ↔ WebSocket for all 3 dashboards
// Run: node server.js
// ==========================================

const express = require('express');
const http = require('http');
const mqtt = require('mqtt');
const WebSocket = require('ws');
const path = require('path');
// Load .env (if present)
require('dotenv').config();
const dbClient = require('./lib/mongodb');

// ==========================================
// CONFIGURATION (env-driven for Docker/PI)
// ==========================================
const HTTP_PORT = parseInt(process.env.HTTP_PORT, 10) || 3000;
const MQTT_BROKER = process.env.MQTT_BROKER || 'mqtt://mosquitto:1883';
const MQTT_CLIENT_ID = process.env.MQTT_CLIENT_ID || ('NodeBackend_' + Math.random().toString(16).slice(2, 8));

// All MQTT topics to monitor
const SENSOR_TOPICS = [
  'tank/waterlevel',
  'tank/turbidity',
  'tank/light',
  'tank/tof',
  'tank/ph'
];
const ACTUATOR_TOPICS = [
  'tank/pump',
  'tank/filter',
  'tank/lamp',
  'tank/stepper'
];
const ALL_TOPICS = [...SENSOR_TOPICS, ...ACTUATOR_TOPICS];

// ==========================================
// EXPRESS + HTTP SERVER
// ==========================================
const app = express();
const server = http.createServer(app);

// Serve static dashboard files
app.use(express.static(path.join(__dirname, 'public')));
app.use(express.json());

// Dashboard routes
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard_all.html'));
});
app.get('/dashboard1', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard1.html'));
});
app.get('/dashboard2', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard2.html'));
});
app.get('/dashboard3', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard3.html'));
});
app.get('/dashboard_camera', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard_camera.html'));
});
app.get('/dashboard4', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard4.html'));
});
app.get('/dashboard5', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard5.html'));
});
app.get('/dashboard_all', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'dashboard_all.html'));
});

// REST API for sending commands
app.post('/api/command', (req, res) => {
  const { topic, message } = req.body;
  if (!topic || !message) {
    return res.status(400).json({ error: 'Missing topic or message' });
  }
  // Validate command topics
  const validCmdTopics = [
    'tank/pump/cmd',
    'tank/filter/cmd',
    'tank/lamp/cmd',
    'tank/stepper/cmd'
  ];
  if (!validCmdTopics.includes(topic)) {
    return res.status(400).json({ error: 'Invalid command topic' });
  }
  // If MQTT is connected, publish immediately. Otherwise queue the command
  // and return a queued response so the dashboard remains responsive.
  if (mqttClient && mqttClient.connected) {
    mqttClient.publish(topic, message, (err) => {
      if (err) {
        console.error('[MQTT] Publish error:', err);
        return res.status(500).json({ error: 'MQTT publish failed' });
      }
      console.log(`[CMD] ${topic} -> ${message}`);
      res.json({ success: true, topic, message });
    });
  } else {
    pendingCommands.push({ topic, message });
    console.log(`[CMD QUEUED] ${topic} -> ${message} (MQTT offline)`);
    res.json({ success: true, queued: true, topic, message });
  }
});

// API: query stored trend points
app.get('/api/trends', async (req, res) => {
  try {
    const { topic, from, to, limit } = req.query;
    const coll = dbClient.getCollection();
    const q = {};
    if (topic) q.topic = topic;
    if (from || to) {
      q.timestamp = {};
      if (from) q.timestamp.$gte = new Date(from);
      if (to) q.timestamp.$lte = new Date(to);
    }
    const cursor = coll.find(q).sort({ timestamp: 1 });
    if (limit) cursor.limit(parseInt(limit, 10));
    const rows = await cursor.toArray();
    res.json(rows);
  } catch (e) {
    console.error('[API] /api/trends error:', e.message);
    res.status(500).json({ error: 'Failed to query trends' });
  }
});

// ==========================================
// MQTT CLIENT
// ==========================================
// Lazy-initialized MQTT client so the HTTP server can start even when
// the broker is temporarily unreachable. We also keep a small queue
// of outgoing commands so manual control works while reconnecting.
console.log(`[MQTT] MQTT broker configured: ${MQTT_BROKER}`);
let mqttClient = null;
const pendingCommands = [];

function initMQTT() {
  if (mqttClient && mqttClient.connected) return;
  console.log(`[MQTT] Connecting to ${MQTT_BROKER}...`);
  mqttClient = mqtt.connect(MQTT_BROKER, {
    clientId: MQTT_CLIENT_ID,
    reconnectPeriod: 5000,
    connectTimeout: 10000
  });

  mqttClient.on('connect', () => {
    console.log('[MQTT] Connected to broker!');
    // Subscribe to all sensor + actuator topics
    ALL_TOPICS.forEach(topic => {
      mqttClient.subscribe(topic, (err) => {
        if (err) {
          console.error(`[MQTT] Subscribe error for ${topic}:`, err);
        } else {
          console.log(`[MQTT] Subscribed: ${topic}`);
        }
      });
    });

    // publish any queued commands
    while (pendingCommands.length) {
      const cmd = pendingCommands.shift();
      mqttClient.publish(cmd.topic, cmd.message, (err) => {
        if (err) console.error('[MQTT] Failed to publish queued command:', err);
        else console.log(`[MQTT QUEUED] ${cmd.topic} -> ${cmd.message}`);
      });
    }
  });

  mqttClient.on('message', (topic, payload) => {
    const value = payload.toString();
    // Debug log incoming MQTT messages for troubleshooting dashboard updates
    console.log(`[MQTT RECV] ${topic} -> ${value}`);
    latestState[topic] = value;

    // Broadcast to all connected WebSocket clients
    const msg = JSON.stringify({ topic, value, timestamp: Date.now() });
    // Debug: show outgoing WS broadcast details
    console.log(`[WS SEND] clients=${wss.clients.size} -> ${msg}`);
    wss.clients.forEach(client => {
      if (client.readyState === WebSocket.OPEN) {
        try {
          client.send(msg);
        } catch (e) {
          console.error('[WS] send error:', e && e.message ? e.message : e);
        }
      }
    });
  });

  mqttClient.on('error', (err) => {
    console.error('[MQTT] Error:', err.message);
  });

  mqttClient.on('reconnect', () => {
    console.log('[MQTT] Reconnecting...');
  });
}

// Latest state cache (sent to new WebSocket clients)
const latestState = {};
// Note: MQTT event handlers are attached inside `initMQTT()` when
// the client is created. Avoid calling `mqttClient.on(...)` here
// because `mqttClient` may be null during module initialization.

// ==========================================
// PERIODIC DB SNAPSHOT (every 30 seconds)
// ==========================================
// Instead of saving every MQTT message, we save a single snapshot
// of all current sensor values every 30 seconds.
const DB_SNAPSHOT_INTERVAL = 30000; // 30 seconds

setInterval(() => {
  // Only save if we have sensor data
  const topics = Object.keys(latestState);
  if (topics.length === 0) return;

  try {
    const coll = dbClient.getCollection();
    const now = new Date();

    // Build one document per topic with its current value
    const docs = topics.map(topic => {
      const raw = latestState[topic];
      const numeric = Number(parseFloat(raw));
      return {
        topic,
        raw,
        value: Number.isFinite(numeric) ? numeric : null,
        timestamp: now,
        source: 'snapshot'
      };
    });

    coll.insertMany(docs).then(result => {
      console.log(`[DB] Snapshot saved: ${result.insertedCount} topics at ${now.toISOString()}`);
    }).catch(err => {
      console.error('[DB] Snapshot insert error:', err && err.message ? err.message : err);
    });
  } catch (e) {
    // DB not ready — skip this cycle
    console.error('[DB] Snapshot skipped:', e && e.message ? e.message : e);
  }
}, DB_SNAPSHOT_INTERVAL);

// ==========================================
// WEBSOCKET SERVER
// ==========================================
const wss = new WebSocket.Server({ server, path: '/ws' });

wss.on('connection', (ws) => {
  console.log('[WS] Client connected. Total:', wss.clients.size);

  // Send current state snapshot to new client
  Object.entries(latestState).forEach(([topic, value]) => {
    ws.send(JSON.stringify({ topic, value, timestamp: Date.now(), cached: true }));
  });

  // Handle commands from dashboard via WebSocket
  ws.on('message', (data) => {
    try {
      const cmd = JSON.parse(data.toString());
      if (cmd.type === 'command' && cmd.topic && cmd.message) {
        if (mqttClient && mqttClient.connected) {
          mqttClient.publish(cmd.topic, cmd.message);
          console.log(`[WS CMD] ${cmd.topic} -> ${cmd.message}`);
        } else {
          pendingCommands.push({ topic: cmd.topic, message: cmd.message });
          console.log(`[WS CMD QUEUED] ${cmd.topic} -> ${cmd.message} (MQTT offline)`);
        }
      }
    } catch (e) {
      console.error('[WS] Invalid message:', e.message);
    }
  });

  ws.on('close', () => {
    console.log('[WS] Client disconnected. Total:', wss.clients.size);
  });
});

// ==========================================
// START SERVER
// ==========================================
server.listen(HTTP_PORT, () => {
  console.log('='.repeat(50));
  console.log('  Tank Monitor Dashboard Server');
  console.log('='.repeat(50));
  console.log(`  HTTP Server:  http://localhost:${HTTP_PORT}`);
  console.log(`  Dashboard 1:  http://localhost:${HTTP_PORT}/dashboard1`);
  console.log(`  Dashboard 2:  http://localhost:${HTTP_PORT}/dashboard2`);
  console.log(`  Dashboard 3:  http://localhost:${HTTP_PORT}/dashboard3`);
  console.log(`  Camera Dash:  http://localhost:${HTTP_PORT}/dashboard_camera`);
  console.log(`  Chemistry:    http://localhost:${HTTP_PORT}/dashboard4`);
  console.log(`  Light Motion: http://localhost:${HTTP_PORT}/dashboard5`);
  console.log(`  Overview:     http://localhost:${HTTP_PORT}/dashboard_all`);
  console.log(`  WebSocket:    ws://localhost:${HTTP_PORT}/ws`);
  console.log(`  MQTT Broker:  ${MQTT_BROKER}`);
  console.log('='.repeat(50));
});

// Start MQTT after HTTP server is listening so dashboards remain available
// Connect to MongoDB first (if configured), then start MQTT. If DB fails
// to connect we'll still start MQTT but inserts will be skipped.
const MONGODB_URI = process.env.MONGODB_URI || 'mongodb://localhost:27017';
const MONGODB_DB = process.env.MONGODB_DB || 'micro';
const MONGODB_COLLECTION = process.env.MONGODB_COLLECTION || 'measurements';

dbClient.connect(MONGODB_URI, MONGODB_DB, MONGODB_COLLECTION)
  .then(() => {
    console.log('[DB] Connected to MongoDB');
  })
  .catch((err) => {
    console.error('[DB] Connection failed:', err && err.message ? err.message : err);
  })
  .finally(() => {
    // Initialize MQTT regardless of DB outcome so dashboards remain functional
    initMQTT();
  });
