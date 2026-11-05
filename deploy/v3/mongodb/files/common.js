const fs = require('fs');
function secret(name) {
  const value = fs.readFileSync('/credentials/' + name + '-password', 'utf8').trim();
  if (!/^[a-f0-9]{48}$/.test(value)) throw new Error('Invalid credential format');
  return value;
}
function uri(host, user, database, credential) {
  const auth = user ? encodeURIComponent(user) + ':' + encodeURIComponent(secret(credential)) + '@' : '';
  return 'mongodb://' + auth + host + ':27017/' + database +
    '?authSource=' + database + '&tls=true&tlsCAFile=%2Fcredentials%2Fca.crt&directConnection=true&retryWrites=false&serverSelectionTimeoutMS=3000&connectTimeoutMS=3000';
}
function safely(action) {
  try { action(); } catch (error) {
    // Driver errors can contain a connection URI. Never print messages or stacks.
    print('MongoDB check failed; code=' + (Number(error.code) || 0));
    quit(1);
  }
}
