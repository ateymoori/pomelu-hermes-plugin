// Pomelu push relay: passes a sealed message from a Hermes plugin to Apple
// (APNs) or Google (FCM). It holds the app's push keys, so each Hermes needs
// none. It cannot read the message (the phone's own key seals it) and
// stores nothing.

const MAX_BODY = 8 * 1024;
const MAX_DATA = 3500;
const APNS_JWT_TTL_MS = 50 * 60 * 1000; // Apple: renew between 20 and 60 min
const FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging';
const EXPIRY_S = 24 * 60 * 60;

/** What the plugin sends, checked; a string says what is wrong. */
export function parsePush(body) {
  if (!body || typeof body !== 'object') return 'body must be a JSON object';
  const { platform, token, data, sandbox = false, collapse } = body;
  if (platform !== 'ios' && platform !== 'android') return 'platform must be ios or android';
  if (typeof token !== 'string') return 'token missing';
  if (platform === 'ios' && !/^[0-9a-f]{64,200}$/i.test(token)) return 'bad APNs token';
  if (platform === 'android' && !/^[\w:-]{32,400}$/.test(token)) return 'bad FCM token';
  if (typeof data !== 'string' || data.length > MAX_DATA || !/^[A-Za-z0-9+/=]+$/.test(data)) {
    return 'data must be base64, at most 3500 characters';
  }
  if (typeof sandbox !== 'boolean') return 'sandbox must be true or false';
  if (collapse !== undefined && !/^[A-Za-z0-9]{1,64}$/.test(collapse)) return 'bad collapse id';
  return { platform, token, data, sandbox, collapse };
}

const json = (status, value) => Response.json(value, { status });

/** One request. [fetcher] reaches Apple and Google (a fake in tests). */
export async function handle(request, env, fetcher = fetch) {
  const url = new URL(request.url);
  if (url.pathname === '/' && request.method === 'GET') {
    return new Response('Pomelu push relay. Source: github.com/ateymoori/pomelu-hermes-plugin\n');
  }
  if (url.pathname !== '/v1/push') return json(404, { error: 'not found' });
  if (request.method !== 'POST') return json(405, { error: 'POST only' });
  const text = await request.text();
  if (text.length > MAX_BODY) return json(413, { error: 'too large' });
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return json(400, { error: 'not JSON' });
  }
  const push = parsePush(body);
  if (typeof push === 'string') return json(400, { error: push });
  if (env.LIMIT) {
    const { success } = await env.LIMIT.limit({ key: push.token });
    if (!success) return json(429, { error: 'too many pushes for this device' });
  }
  return push.platform === 'ios' ? sendApns(push, env, fetcher) : sendFcm(push, env, fetcher);
}

// ------------------------------------------------------------------ APNs

let apnsJwt = null;

async function apnsToken(env) {
  if (apnsJwt && Date.now() - apnsJwt.at < APNS_JWT_TTL_MS) return apnsJwt.value;
  const key = await importPkcs8(env.APNS_KEY, { name: 'ECDSA', namedCurve: 'P-256' });
  const value = await signJwt(
    { alg: 'ES256', kid: env.APNS_KEY_ID },
    { iss: env.APNS_TEAM_ID, iat: nowS() },
    key,
    { name: 'ECDSA', hash: 'SHA-256' },
  );
  apnsJwt = { value, at: Date.now() };
  return value;
}

async function sendApns(push, env, fetcher) {
  const host = push.sandbox ? 'api.sandbox.push.apple.com' : 'api.push.apple.com';
  const headers = {
    authorization: `bearer ${await apnsToken(env)}`,
    'apns-topic': env.APNS_TOPIC,
    'apns-push-type': 'alert',
    'apns-priority': '10',
    'apns-expiration': String(nowS() + EXPIRY_S),
  };
  if (push.collapse) headers['apns-collapse-id'] = push.collapse;
  // The app's notification extension opens `enc` and writes the real text;
  // this alert shows only if it cannot.
  const payload = {
    aps: { alert: { title: 'Pomelu', body: 'New message' }, sound: 'default', 'mutable-content': 1 },
    enc: push.data,
  };
  const res = await fetcher(`https://${host}/3/device/${push.token}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
  if (res.ok) return json(200, { ok: true });
  const reason = (await res.json().catch(() => ({}))).reason ?? '';
  if (res.status === 410 || ['BadDeviceToken', 'DeviceTokenNotForTopic', 'Unregistered'].includes(reason)) {
    return json(410, { error: 'device gone', reason });
  }
  if (reason === 'ExpiredProviderToken') apnsJwt = null;
  return json(502, { error: 'apns refused', status: res.status, reason });
}

// ------------------------------------------------------------------- FCM

let googleToken = null;

async function fcmAccessToken(account, fetcher) {
  if (googleToken && Date.now() < googleToken.until) return googleToken.value;
  const key = await importPkcs8(account.private_key, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' });
  const iat = nowS();
  const assertion = await signJwt(
    { alg: 'RS256', typ: 'JWT' },
    { iss: account.client_email, scope: FCM_SCOPE, aud: account.token_uri, iat, exp: iat + 3600 },
    key,
    { name: 'RSASSA-PKCS1-v1_5' },
  );
  const res = await fetcher(account.token_uri, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: `grant_type=${encodeURIComponent('urn:ietf:params:oauth:grant-type:jwt-bearer')}&assertion=${assertion}`,
  });
  if (!res.ok) throw new Error(`google token ${res.status}`);
  const { access_token: value, expires_in: ttl } = await res.json();
  googleToken = { value, until: Date.now() + (ttl - 300) * 1000 };
  return value;
}

async function sendFcm(push, env, fetcher) {
  const account = JSON.parse(env.FCM_SERVICE_ACCOUNT);
  let access;
  try {
    access = await fcmAccessToken(account, fetcher);
  } catch (e) {
    return json(502, { error: 'fcm sign-in failed', reason: String(e.message) });
  }
  const message = {
    token: push.token,
    // Data only: the app opens it and posts the notification itself.
    data: { enc: push.data },
    android: { priority: 'HIGH', ttl: `${EXPIRY_S}s` },
  };
  if (push.collapse) message.android.collapse_key = push.collapse;
  const res = await fetcher(`https://fcm.googleapis.com/v1/projects/${account.project_id}/messages:send`, {
    method: 'POST',
    headers: { authorization: `Bearer ${access}`, 'content-type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (res.ok) return json(200, { ok: true });
  const error = (await res.json().catch(() => ({}))).error ?? {};
  const code = (error.details ?? []).map((d) => d.errorCode).find(Boolean) ?? error.status ?? '';
  if (res.status === 404 || code === 'UNREGISTERED') return json(410, { error: 'device gone', reason: code });
  if (res.status === 401) googleToken = null;
  return json(502, { error: 'fcm refused', status: res.status, reason: code });
}

// ------------------------------------------------------------------ JWT

const nowS = () => Math.floor(Date.now() / 1000);

const b64url = (bytes) =>
  btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

const b64urlText = (value) => b64url(new TextEncoder().encode(JSON.stringify(value)));

async function importPkcs8(pem, algorithm) {
  const der = Uint8Array.from(atob(pem.replace(/-----[^-]+-----|\s/g, '')), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey('pkcs8', der, algorithm, false, ['sign']);
}

async function signJwt(header, claims, key, algorithm) {
  const input = `${b64urlText(header)}.${b64urlText(claims)}`;
  const signature = await crypto.subtle.sign(algorithm, key, new TextEncoder().encode(input));
  return `${input}.${b64url(signature)}`;
}

/** Forgets the cached sign-ins (tests). */
export function resetTokens() {
  apnsJwt = null;
  googleToken = null;
}
