const { MongoClient } = require('mongodb');

let client = null;
let database = null;
let collectionName = 'measurements';

async function connect(uri, dbName = 'micro', collName = 'measurements') {
  if (!uri) throw new Error('MONGODB_URI is required');
  collectionName = collName;
  client = new MongoClient(uri);
  await client.connect();
  database = client.db(dbName);

  const coll = database.collection(collectionName);
  // Ensure indexes: topic+timestamp for queries, and TTL on timestamp (30 days)
  try {
    await coll.createIndex({ topic: 1, timestamp: 1 });
    await coll.createIndex({ timestamp: 1 }, { expireAfterSeconds: 2592000 });
  } catch (e) {
    console.error('[DB] index creation error:', e.message);
  }

  return coll;
}

function getCollection() {
  if (!database) throw new Error('Database not connected. Call connect() first.');
  return database.collection(collectionName);
}

function close() {
  if (client) return client.close();
  return Promise.resolve();
}

module.exports = {
  connect,
  getCollection,
  close
};
