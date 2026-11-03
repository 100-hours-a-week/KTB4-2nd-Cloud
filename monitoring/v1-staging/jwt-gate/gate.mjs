// V1 edge admission only. BE still validates SID, user state and authorization.
// No request token, signing key, user ID or exception text is ever logged.
const MAX_TOKEN_BYTES = 4096;
const MAX_USER_ID = '9223372036854775807';

function configuration(config) {
    if (!config || typeof config.secret !== 'string' || Buffer.byteLength(config.secret) < 32
        || typeof config.issuer !== 'string' || !config.issuer
        || typeof config.audience !== 'string' || !config.audience
        || config.clockSkewSeconds !== 60) {
        throw new Error('Invalid edge configuration');
    }
    return config;
}

function accessCookie(headers) {
    let found = null;
    for (let i = 0; i < headers.length; i++) {
        const h = headers[i];
        if (h[0].toLowerCase() !== 'cookie') continue;
        const parts = h[1].split(';');
        for (let j = 0; j < parts.length; j++) {
            const part = parts[j];
            const split = part.indexOf('=');
            if (split < 0 || part.slice(0, split).trim() !== 'accessToken') continue;
            // Ambiguous duplicate cookies are rejected, not selected by position.
            if (found !== null) return {reason: 'malformed'};
            found = part.slice(split + 1).trim();
        }
    }
    return found ? {token: found} : {reason: 'missing'};
}

function segment(value) {
    if (!value || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
        throw new Error('Malformed JWT');
    }
    const bytes = Buffer.from(value, 'base64url');
    if (bytes.toString('base64url') !== value) throw new Error('Noncanonical JWT');
    return bytes;
}

function validUserId(value) {
    return typeof value === 'string' && /^[1-9][0-9]{0,18}$/.test(value)
        && (value.length < MAX_USER_ID.length || value <= MAX_USER_ID);
}

async function verify(token, config, now) {
    configuration(config); // Configuration failures must not become invalid-token responses.
    if (typeof token !== 'string' || Buffer.byteLength(token) > MAX_TOKEN_BYTES) return {reason: 'malformed'};
    let fields, header, claims, signature;
    try {
        fields = token.split('.');
        if (fields.length !== 3) return {reason: 'malformed'};
        header = JSON.parse(segment(fields[0]).toString('utf8'));
        claims = JSON.parse(segment(fields[1]).toString('utf8'));
        signature = segment(fields[2]);
        if (!header || Array.isArray(header) || !claims || Array.isArray(claims)
            || typeof header !== 'object' || typeof claims !== 'object') return {reason: 'malformed'};
    } catch (_) { return {reason: 'malformed'}; }
    // Never select keys or algorithms from the request. Unencoded/critical extensions are unsupported.
    if (header.alg !== 'HS256' || header.crit !== undefined || header.b64 !== undefined
        || (header.typ !== undefined && header.typ !== 'JWT') || signature.length !== 32) {
        return {reason: 'malformed'};
    }
    const key = await crypto.subtle.importKey('raw', Buffer.from(config.secret, 'utf8'),
        {name: 'HMAC', hash: 'SHA-256'}, false, ['verify']);
    const signed = Buffer.from(fields[0] + '.' + fields[1], 'utf8');
    if (!(await crypto.subtle.verify('HMAC', key, signature, signed))) return {reason: 'bad_signature'};
    if (!Number.isSafeInteger(claims.exp) || claims.exp <= 0
        || (claims.nbf !== undefined && !Number.isSafeInteger(claims.nbf))) return {reason: 'bad_claims'};
    if (now > claims.exp + config.clockSkewSeconds) return {reason: 'expired'};
    if (claims.nbf !== undefined && now + config.clockSkewSeconds < claims.nbf) return {reason: 'not_yet_valid'};
    const audience = typeof claims.aud === 'string' ? [claims.aud] : claims.aud;
    if (claims.iss !== config.issuer || !Array.isArray(audience)
        || !audience.every(a => typeof a === 'string') || audience.indexOf(config.audience) < 0
        || !validUserId(claims.sub) || typeof claims.sid !== 'string'
        || !claims.sid.trim() || claims.sid.length > 256) return {reason: 'bad_claims'};
    // This signed identity is a limiter key, not a declaration of a live login session.
    return {reason: 'signature_valid', userKey: 'u:' + claims.sub};
}

async function gate(r) {
    r.variables.yeodam_jwt_user_key = '';
    r.variables.yeodam_jwt_gate_result = 'error';
    try {
        const config = configuration(yeodam_jwt_config);
        const cookie = accessCookie(r.rawHeadersIn);
        const decision = cookie.reason ? cookie : await verify(cookie.token, config, Date.now() / 1000);
        r.variables.yeodam_jwt_gate_result = decision.reason;
        if (!decision.userKey) { r.internalRedirect('@yeodam_jwt_reject'); return; }
        r.variables.yeodam_jwt_user_key = decision.userKey;
        r.internalRedirect(r.uri.indexOf('/api/trips') === 0
            ? '@yeodam_jwt_user_trips' : '@yeodam_jwt_user_api');
    } catch (_) {
        // Fail closed on key/configuration/crypto failures. Never use an IP exemption fallback.
        r.variables.yeodam_jwt_user_key = '';
        r.variables.yeodam_jwt_gate_result = 'error';
        r.headersOut['Cache-Control'] = 'no-store';
        r.return(503, '{"message":"AUTH_GATE_UNAVAILABLE","data":null}');
    }
}

function reject(r) {
    r.headersOut['Content-Type'] = 'application/json';
    r.headersOut['Cache-Control'] = 'no-store';
    r.return(401, '{"message":"UNAUTHORIZED","data":null}');
}

export default {gate, reject, verify, accessCookie, validUserId};
