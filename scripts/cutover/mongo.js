// Public program only. Credentials stay in the MongoDB Pod; documents travel on stdin/stdout privately.
load('/runtime/common.js');
safely(() => {
  const db = new Mongo(uri('127.0.0.1', 'cloud_admin', 'admin', 'admin')).getDB('yeodam');
  const collections = db.getCollectionInfos().filter(c => c.type === 'collection' && !c.name.startsWith('system.'));
  if (process.env.CUTOVER_ACTION === 'backup') {
    const data = collections.map(c => ({name:c.name,indexes:db.getCollection(c.name).getIndexes(),documents:db.getCollection(c.name).find().toArray()}));
    print(EJSON.stringify(data, null, 0, {relaxed:false}));
  } else if (process.env.CUTOVER_ACTION === 'clear') {
    for (const c of collections) db.getCollection(c.name).deleteMany({});
    if (collections.some(c => db.getCollection(c.name).countDocuments({}) !== 0)) throw new Error('Clear incomplete');
    print('CLEARED');
  } else if (process.env.CUTOVER_ACTION === 'verify-stories') {
    const ids = JSON.parse(fs.readFileSync(0, 'utf8'));
    if (!Array.isArray(ids) || ids.some(id => !/^[0-9]+$/.test(id))) throw new Error('Invalid story identifiers');
    const docs = db.getCollection('story_contents').find({_id:{$in:ids.map(id => Long(id))}}).toArray();
    if (docs.length !== ids.length || docs.some(d => !Array.isArray(d.blocks) || !Array.isArray(d.selectedPlaceIds) || typeof d.storySummary !== 'string')) throw new Error('Story backfill incomplete');
    print(JSON.stringify({expected:ids.length,found:docs.length,missing:0}));
  } else if (process.env.CUTOVER_ACTION === 'restore') {
    const data = EJSON.parse(fs.readFileSync(0, 'utf8'), {relaxed:false});
    if (!Array.isArray(data) || data.some(c => typeof c.name !== 'string' || c.name.startsWith('system.'))) throw new Error('Invalid archive');
    for (const c of collections) db.getCollection(c.name).deleteMany({});
    for (const c of data) if (c.documents.length) db.getCollection(c.name).insertMany(c.documents);
    for (const c of data) if (db.getCollection(c.name).countDocuments({}) !== c.documents.length) throw new Error('Restore incomplete');
    print('RESTORED');
  } else { throw new Error('Unknown action'); }
});
