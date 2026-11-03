import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import gate from '../../monitoring/v1-staging/jwt-gate/gate.mjs';
const config = {secret: 'synthetic-only-key-for-isolated-test-468', issuer: 'https://issuer.test', audience: 'yeodam-api', clockSkewSeconds: 60};
const now = 1800000000;
const claims = {iss: config.issuer, aud: [config.audience], sub: '123', sid: 'session-one', iat: now - 10, exp: now + 60};
function token(data = claims, header = {alg: 'HS256', typ: 'JWT'}, secret = config.secret) {
    const h = Buffer.from(JSON.stringify(header)).toString('base64url');
    const p = Buffer.from(JSON.stringify(data)).toString('base64url');
    return h + '.' + p + '.' + createHmac('sha256', secret).update(h + '.' + p).digest('base64url');
}
test('signed identity shares a user key across SID without numeric precision loss', async () => {
    for (let sid of ['session-one', 'session-two']) assert.deepEqual(await gate.verify(token({...claims, sid}), config, now), {reason:'signature_valid', userKey:'u:123'});
    assert.equal((await gate.verify(token({...claims, sub:'9223372036854775807'}), config, now)).userKey, 'u:9223372036854775807');
});
test('tampered signature, algorithm confusion and unsigned input cannot produce a user key', async () => {
    for (let t of [token(claims, {alg:'none'}), token(claims, {alg:'HS512'}), token(claims, {alg:'HS256',crit:['b64'],b64:false}), token(claims, undefined, 'wrong-secret'), 'a.b.c', 'x'.repeat(4097)]) {
        assert.equal((await gate.verify(t, config, now)).userKey, undefined);
    }
});
test('issuer/audience/expiry/not-before/subject/SID are checked after signature', async () => {
    for (let patch of [{iss:'wrong'}, {aud:['wrong']}, {aud:null}, {exp:now-61}, {nbf:now+61}, {sub:'0'}, {sub:'01'}, {sub:'9223372036854775808'}, {sub:123}, {sid:''}, {exp:'tomorrow'}]) {
        assert.equal((await gate.verify(token({...claims,...patch}),config,now)).userKey,undefined,JSON.stringify(patch));
    }
    assert.equal((await gate.verify(token({...claims,exp:now-59}),config,now)).userKey,'u:123');
});
test('only the V1 cookie is consumed; duplicate cookies are rejected', () => {
    assert.deepEqual(gate.accessCookie([['Authorization','Bearer anything']]), {reason:'missing'});
    assert.deepEqual(gate.accessCookie([['Cookie','other=x; accessToken=abc']]),{token:'abc'});
    assert.deepEqual(gate.accessCookie([['Cookie','accessToken=a'],['cookie','accessToken=b']]),{reason:'malformed'});
});
test('configuration failures fail closed rather than using invalid-IP fallback', async () => {
    await assert.rejects(gate.verify(token(),{...config,secret:''},now));
    await assert.rejects(gate.verify(token(),{...config,clockSkewSeconds:0},now));
});

test('gate returns 503 with no route fallback when the preloaded key is unavailable', async () => {
    globalThis.yeodam_jwt_config = {...config, secret:''};
    const r = {variables:{}, headersOut:{}, rawHeadersIn:[], return(status){this.status=status;}, internalRedirect(route){this.route=route;}};
    try { await gate.gate(r); assert.equal(r.status,503); assert.equal(r.route,undefined); assert.equal(r.variables.yeodam_jwt_user_key,''); }
    finally { delete globalThis.yeodam_jwt_config; }
});
