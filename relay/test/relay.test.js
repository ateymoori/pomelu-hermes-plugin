import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import { handle, resetTokens } from '../src/relay.js';

const pem = async (key) =>
  `-----BEGIN PRIVATE KEY-----\n${Buffer.from(await crypto.subtle.exportKey('pkcs8', key)).toString('base64')}\n-----END PRIVATE KEY-----`;

const ec = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify']);
const rsa = await crypto.subtle.generateKey(
  { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
  true,
  ['sign', 'verify'],
);

const env = {
  APNS_KEY: await pem(ec.privateKey),
  APNS_KEY_ID: 'KEY123',
  APNS_TEAM_ID: 'TEAM123',
  APNS_TOPIC: 'se.royan.pomelu',
  FCM_SERVICE_ACCOUNT: JSON.stringify({
    project_id: 'proj',
    client_email: 'relay@proj.iam.gserviceaccount.com',
    token_uri: 'https://oauth2.googleapis.com/token',
    private_key: await pem(rsa.privateKey),
  }),
};

const iosToken = 'ab'.repeat(32);
const fcmToken = 'f'.repeat(40) + ':APA91b' + 'x'.repeat(100);

/** Answers each call with the next scripted response; keeps the calls. */
function fakeNetwork(...responses) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    const [status, body] = responses.shift() ?? [200, {}];
    return Response.json(body, { status });
  };
  return { calls, fetcher };
}

const post = (body) =>
  new Request('https://pomelu-push.royan.se/v1/push', { method: 'POST', body: JSON.stringify(body) });

const verifyJwt = async (jwt, key, algorithm) => {
  const [h, c, s] = jwt.split('.');
  const sig = Buffer.from(s, 'base64url');
  assert.ok(await crypto.subtle.verify(algorithm, key, sig, new TextEncoder().encode(`${h}.${c}`)));
  return [h, c].map((part) => JSON.parse(Buffer.from(part, 'base64url')));
};

beforeEach(resetTokens);

test('an iOS push goes to APNs, signed with the team key, sealed text inside', async () => {
  const net = fakeNetwork([200, {}]);
  const res = await handle(post({ platform: 'ios', token: iosToken, data: 'c2VhbGVk', collapse: 'abc123' }), env, net.fetcher);
  assert.equal(res.status, 200);
  const [{ url, init }] = net.calls;
  assert.equal(url, `https://api.push.apple.com/3/device/${iosToken}`);
  assert.equal(init.headers['apns-topic'], 'se.royan.pomelu');
  assert.equal(init.headers['apns-collapse-id'], 'abc123');
  const [header, claims] = await verifyJwt(init.headers.authorization.slice(7), ec.publicKey, {
    name: 'ECDSA',
    hash: 'SHA-256',
  });
  assert.deepEqual([header.kid, claims.iss], ['KEY123', 'TEAM123']);
  const payload = JSON.parse(init.body);
  assert.equal(payload.enc, 'c2VhbGVk');
  assert.equal(payload.aps['mutable-content'], 1);
});

test('a debug build pushes through the APNs sandbox', async () => {
  const net = fakeNetwork([200, {}]);
  await handle(post({ platform: 'ios', token: iosToken, data: 'eA==', sandbox: true }), env, net.fetcher);
  assert.match(net.calls[0].url, /^https:\/\/api\.sandbox\.push\.apple\.com\//);
});

test('a device Apple no longer knows answers 410, so the plugin forgets it', async () => {
  const net = fakeNetwork([400, { reason: 'BadDeviceToken' }]);
  const res = await handle(post({ platform: 'ios', token: iosToken, data: 'eA==' }), env, net.fetcher);
  assert.equal(res.status, 410);
});

test('an Android push signs in to Google once, then sends a data-only FCM message', async () => {
  const net = fakeNetwork([200, { access_token: 'at-1', expires_in: 3600 }], [200, {}], [200, {}]);
  for (let i = 0; i < 2; i++) {
    const res = await handle(post({ platform: 'android', token: fcmToken, data: 'eA==' }), env, net.fetcher);
    assert.equal(res.status, 200);
  }
  assert.equal(net.calls.length, 3);
  const assertion = new URLSearchParams(net.calls[0].init.body).get('assertion');
  const [, claims] = await verifyJwt(assertion, rsa.publicKey, { name: 'RSASSA-PKCS1-v1_5' });
  assert.equal(claims.iss, 'relay@proj.iam.gserviceaccount.com');
  const send = net.calls[1];
  assert.equal(send.url, 'https://fcm.googleapis.com/v1/projects/proj/messages:send');
  assert.equal(send.init.headers.authorization, 'Bearer at-1');
  const { message } = JSON.parse(send.init.body);
  assert.deepEqual(message.data, { enc: 'eA==' });
  assert.equal(message.notification, undefined);
  assert.equal(message.android.priority, 'HIGH');
});

test('an unregistered FCM token answers 410', async () => {
  const net = fakeNetwork([200, { access_token: 'at', expires_in: 3600 }], [
    404,
    { error: { status: 'NOT_FOUND', details: [{ errorCode: 'UNREGISTERED' }] } },
  ]);
  const res = await handle(post({ platform: 'android', token: fcmToken, data: 'eA==' }), env, net.fetcher);
  assert.equal(res.status, 410);
});

test('a malformed request is refused before anything leaves', async () => {
  const net = fakeNetwork();
  for (const body of [
    { platform: 'web', token: iosToken, data: 'eA==' },
    { platform: 'ios', token: 'not-hex', data: 'eA==' },
    { platform: 'ios', token: iosToken, data: 'not base64!' },
    { platform: 'ios', token: iosToken, data: 'eA==', collapse: 'bad/id' },
  ]) {
    assert.equal((await handle(post(body), env, net.fetcher)).status, 400);
  }
  assert.equal(net.calls.length, 0);
});

test('a device over its rate limit gets 429', async () => {
  const net = fakeNetwork();
  const limited = { ...env, LIMIT: { limit: async () => ({ success: false }) } };
  const res = await handle(post({ platform: 'ios', token: iosToken, data: 'eA==' }), limited, net.fetcher);
  assert.equal(res.status, 429);
  assert.equal(net.calls.length, 0);
});
