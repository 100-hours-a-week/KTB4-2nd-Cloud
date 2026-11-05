load('/runtime/common.js');
safely(() => {
  const admin = new Mongo(uri('127.0.0.1', 'cloud_health', 'admin', 'health')).getDB('admin');
  const status = admin.runCommand({connectionStatus: 1});
  if (!status.authInfo.authenticatedUsers.some(user => user.user === 'cloud_health')) throw new Error('Not authenticated');
  if (admin.runCommand({ping: 1}).ok !== 1) throw new Error('Not responding');
  print('PASS: authenticated local ping');
});
