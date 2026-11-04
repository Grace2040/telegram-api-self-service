const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sha256 = require('../app/static/sha256.min.js');

const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

function harness(fetch = () => new Promise(() => {}), initialStorage = {}) {
  const elements = new Map();
  function el(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', dataset: {}, disabled: false, style: {},
      classList: { add() {}, remove() {} }, listeners: {},
      addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {},
    });
    return elements.get(id);
  }
  const storage = new Map([['tg_flow', JSON.stringify({ request_id: 'flow', step: 2, expires_at: Date.now() / 1000 + 600 })]]);
  for (const [key, value] of Object.entries(initialStorage)) {
    if (value === null) storage.delete(key);
    else storage.set(key, value);
  }
  const context = {
    document: { getElementById: el },
    localStorage: { getItem() { return null; } },
    sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch, setTimeout() { return 1; }, clearTimeout() {}, console,
    TextEncoder, AbortController, sha256, navigator: {}, window: { isSecureContext: true },
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return { context, el, storage };
}

function response(data = {}, code = 0) {
  return { ok: true, status: 200, async json() { return { code, message: code ? 'synthetic error' : 'ok', data }; } };
}

test('repeated Enter sends a single create request and reset cannot abandon it', async () => {
  const pending = deferred();
  let calls = 0;
  const { context, el, storage } = harness(() => { calls++; return pending.promise; });
  el('code').value = 'AbCD123';
  const first = context.createApp();
  el('code').listeners.keydown({ key: 'Enter' });
  context.reset();
  assert.equal(calls, 1);
  assert.ok(storage.has('tg_flow'));
  pending.resolve(response({ api_id: 123, api_hash: 'a'.repeat(32), existed: true }));
  await first;
  assert.equal(el('applyBtn').disabled, false);
  assert.equal(el('apiId').textContent, 123);
  assert.equal(storage.has('tg_flow'), false);
});

test('a pasted full message is submitted intact', async () => {
  let submitted;
  const { context, el } = harness((url, options) => {
    submitted = JSON.parse(options.body);
    return Promise.resolve(response({ api_id: 123, api_hash: 'a'.repeat(32) }));
  });
  const message = 'Your login code: AbCD123. Do not share this message.';
  el('code').value = message;
  if (el('code').listeners.input) el('code').listeners.input({ target: el('code') });
  await context.createApp();
  assert.equal(submitted.code, message);
  assert.match(html, /id="code"[^>]*maxlength="4096"/);
});

test('empty phone does not leave send permanently locked; repeated send is guarded', async () => {
  const pending = deferred();
  let calls = 0;
  const { context, el } = harness(() => { calls++; return pending.promise; });
  vm.runInContext("requestId = null; pow = {challenge_id:'synthetic', nonce:1};", context);
  await context.sendCode();
  assert.equal(calls, 0);
  el('phone').value = '+12025550123';
  const first = context.sendCode();
  await context.sendCode();
  assert.equal(calls, 1);
  pending.resolve(response({ request_id: 'new-flow' }));
  await first;
  assert.equal(calls, 1); // Second step must not request an unused PoW challenge.
});

test('create failure unlocks the UI for a deliberate retry', async () => {
  let calls = 0;
  const { context, el } = harness(() => { calls++; return Promise.resolve(response({}, 2002)); });
  el('code').value = 'AbCD123';
  await context.createApp();
  assert.equal(el('applyBtn').disabled, false);
  el('code').value = 'New123';
  await context.createApp();
  assert.equal(calls, 2);
});

test('PoW uses the bundled SHA-256 library', async () => {
  assert.equal(sha256('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  const { context, el, storage } = harness(() => Promise.resolve(response({
    challenge_id: 'synthetic', salt: 'salt', challenge: sha256('salt3'), maxnumber: 10,
  })));
  await context.newPowChallenge();
  assert.equal(el('sendBtn').disabled, false);
  assert.equal(JSON.parse(storage.get('tg_pow')).nonce, 3);
});

test('missing SHA-256 library fails explicitly without another implementation', async () => {
  const { context, el } = harness(() => Promise.resolve(response({
    challenge_id: 'synthetic', salt: 'salt', challenge: sha256('salt3'), maxnumber: 10,
  })));
  delete context.sha256;
  await context.newPowChallenge();
  assert.equal(el('sendBtn').disabled, true);
  assert.match(el('powHint').textContent, /组件加载失败/);
});

test('a stale PoW response cannot overwrite the latest challenge', async () => {
  const first = deferred(), second = deferred();
  let calls = 0;
  const { context, storage } = harness(() => (++calls === 1 ? first.promise : second.promise));
  const old = context.newPowChallenge();
  const current = context.newPowChallenge();
  second.resolve(response({ challenge_id: 'current', salt: 'salt', challenge: sha256('salt3'), maxnumber: 10 }));
  await current;
  first.resolve(response({ challenge_id: 'old', salt: 'salt', challenge: sha256('salt3'), maxnumber: 10 }));
  await old;
  assert.equal(JSON.parse(storage.get('tg_pow')).challenge_id, 'current');
});

test('copy uses only Clipboard API and reports permission failures', async () => {
  const { context, el } = harness();
  el('apiId').textContent = '123';
  context.copyVal('apiId');
  assert.match(el('error').textContent, /HTTPS/);
  context.navigator.clipboard = { writeText: () => Promise.reject(new Error('denied')) };
  context.copyVal('apiId');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(el('error').textContent, /剪贴板权限/);
});

test('cached PoW uses server expires_at rather than a separate fixed duration', () => {
  let calls = 0;
  const saved = { challenge_id: 'cached', nonce: 3, expires_at: Date.now() / 1000 + 45 };
  harness(() => { calls++; return new Promise(() => {}); }, { tg_flow: null, tg_pow: JSON.stringify(saved) });
  assert.equal(calls, 0);
  saved.expires_at = Date.now() / 1000 - 1;
  saved.ts = Date.now(); // A recent legacy timestamp cannot override server expiry.
  harness(() => { calls++; return new Promise(() => {}); }, { tg_flow: null, tg_pow: JSON.stringify(saved) });
  assert.equal(calls, 1);
});

test('expired restored flow is cleared and starts at the phone stage', () => {
  let calls = 0;
  const { storage } = harness(() => { calls++; return new Promise(() => {}); }, {
    tg_flow: JSON.stringify({ request_id: 'expired', step: 2, expires_at: Date.now() / 1000 - 1 }),
  });
  assert.equal(calls, 1);
  assert.equal(storage.has('tg_flow'), false);
});
