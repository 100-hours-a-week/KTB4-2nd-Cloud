load('/runtime/common.js');
safely(() => {
  if (new Mongo(uri('127.0.0.1', '', 'admin', '')).getDB('admin').runCommand({ping: 1}).ok !== 1) throw new Error('Not ready');
});
