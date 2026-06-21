const mqtt = require('mqtt');
const client = mqtt.connect('mqtt://localhost:1883');
let messageCount = 0;

console.log('🔍 Listening for MQTT messages on all topics...\n');

client.on('connect', () => {
  client.subscribe('tank/#', (err) => {
    if (!err) console.log('✓ Subscribed to tank/# - waiting for data...\n');
  });
});

client.on('message', (topic, message) => {
  messageCount++;
  const value = message.toString();
  const time = new Date().toLocaleTimeString();
  console.log(`[${time}] [${messageCount}] ${topic} → ${value}`);
});

// Run for 30 seconds then exit
setTimeout(() => {
  console.log(`\n\n✓ Listened for 30 seconds. Received ${messageCount} messages.`);
  if (messageCount === 0) {
    console.log('❌ No data received - Arduino may not be publishing.');
  }
  client.end();
  process.exit(0);
}, 30000);
