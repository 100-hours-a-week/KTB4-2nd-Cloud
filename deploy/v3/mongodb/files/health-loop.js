load('/runtime/common.js');
const directory = '/tmp/mongodb-health';
let client = null;
while (true) {
  let healthy = 0;
  try {
    if (!client) client = new Mongo(uri('127.0.0.1', 'cloud_health', 'admin', 'health') + '&socketTimeoutMS=3000');
    const admin = client.getDB('admin');
    const status = admin.runCommand({connectionStatus: 1});
    if (!status.authInfo.authenticatedUsers.some(user => user.user === 'cloud_health' && user.db === 'admin')) throw new Error('Not authenticated');
    if (admin.runCommand({ping: 1}).ok !== 1) throw new Error('Not responding');
    healthy = 1;
  } catch (_) {
    // URI/driver errors can contain credentials. Only publish a numeric failure.
    if (client) { try { client.close(); } catch (_) {} }
    client = null;
  }
  fs.writeFileSync(directory + '/status.next', `${Math.floor(Date.now() / 1000)} ${process.pid} ${healthy}\n`, {mode: 0o600});
  fs.renameSync(directory + '/status.next', directory + '/status');
  sleep(5000);
}
