load('/runtime/common.js');
safely(() => {
  const connection = new Mongo(uri('127.0.0.1', '', 'admin', ''));
  if (connection.getDB('admin').runCommand({ping: 1}).ok !== 1) throw new Error('Not ready');
  connection.getDB('admin').createUser({user: 'cloud_admin', pwd: secret('admin'), roles: ['root'], mechanisms: ['SCRAM-SHA-256']});
  connection.getDB('yeodam').createUser({user: 'app', pwd: secret('app'), roles: [{role: 'readWrite', db: 'yeodam'}], mechanisms: ['SCRAM-SHA-256']});
  connection.getDB('admin').createUser({user: 'cloud_health', pwd: secret('health'), roles: ['clusterMonitor'], mechanisms: ['SCRAM-SHA-256']});
  fs.writeFileSync('/tmp/accounts-created', 'ready', {mode: 0o600});
  print('PASS: local TLS account initialization');
});
