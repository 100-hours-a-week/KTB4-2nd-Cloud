load('/runtime/common.js');
safely(() => {
  const host = 'mongodb.mongodb.svc.cluster.local';
  const connection = new Mongo(uri(host, 'app', 'yeodam', 'app'));
  const database = connection.getDB('yeodam');
  const name = '_cloud_check_525_' + new ObjectId().toHexString();
  const collection = database.getCollection(name);
  try {
    collection.insertOne({_id: 525, version: NumberLong('0'), storySummary: 'synthetic', blocks: [{id: NumberLong('1'), text: 'trial'}]}, {writeConcern: {w: 1, j: true}});
    if (collection.findOne({_id: 525}).blocks[0].text !== 'trial') throw new Error('Read mismatch');
    let duplicate = false;
    try { collection.insertOne({_id: 525}); } catch (error) { duplicate = error.code === 11000; }
    if (!duplicate) throw new Error('Duplicate accepted');
    const first = collection.updateOne({_id: 525, version: NumberLong('0')}, {$set: {storySummary: 'updated'}, $inc: {version: NumberLong('1')}});
    const stale = collection.updateOne({_id: 525, version: NumberLong('0')}, {$set: {storySummary: 'must-not-overwrite'}});
    if (first.modifiedCount !== 1 || stale.matchedCount !== 0) throw new Error('Version guard failed');
    let denied = false;
    try {
      const result = connection.getDB('admin').runCommand({createUser: 'must-not-exist', pwd: 'synthetic', roles: []});
      denied = result.ok !== 1 && result.code === 13;
    } catch (error) { denied = error.code === 13; }
    if (!denied) throw new Error('App administrative permission widened');
    let anonymousDenied = false;
    try { new Mongo(uri(host, '', 'yeodam', '')).getDB('yeodam').getCollection(name).findOne({}); }
    catch (error) { anonymousDenied = error.code === 13; }
    if (!anonymousDenied) throw new Error('Anonymous document access accepted');
    let rejected = false;
    try {
      const invalid = uri(host, 'app', 'yeodam', 'app').replace(secret('app'), 'synthetic-invalid');
      new Mongo(invalid).getDB('yeodam').getCollection(name).findOne({});
    } catch (error) { rejected = error.code === 18; }
    if (!rejected) throw new Error('Bad password not rejected');
  } finally { collection.drop(); }
  print('PASS: TLS/app CRUD, duplicate rejection, version-conditional conflict, privilege/auth rejection and cleanup');
});
