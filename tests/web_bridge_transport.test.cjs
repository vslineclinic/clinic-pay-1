const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web_static/bridge-api.js'), 'utf8');

function environment(embedded = true) {
  const messages = [], listeners = {}, timers = new Map(); let timerId = 0;
  const parent = {postMessage: (message, origin) => messages.push({message, origin})};
  const window = {location: {search: embedded ? '?streamlitUrl=https%3A%2F%2Fclinic.invalid%2F' : ''},
    parent, addEventListener: (event, callback) => { listeners[event] = callback; }};
  if (!embedded) window.parent = window;
  const context = {window, URLSearchParams, URL, Blob, crypto: webcrypto,
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id)};
  vm.runInNewContext(source, context);
  // Match index.html order: the standalone API adapter must not replace the bridge.
  const bridge = window.LedgerTransport;
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../web_static/api.js'), 'utf8'), context);
  if (embedded) assert.equal(window.LedgerTransport, bridge);
  const render = (args, origin = 'https://clinic.invalid', source = parent) => listeners.message({
    source, origin, data: {type: 'streamlit:render', args},
  });
  const requests = () => messages.filter(item => item.message.type === 'streamlit:setComponentValue');
  return {window, messages, render, requests, timers};
}

async function main() {
  assert.equal(environment(false).window.LedgerTransport.mode, 'live');
  const env = environment();
  assert.equal(env.messages.length, 2, 'Bootstrap only announces the component and its height.');
  const api = env.window.LedgerTransport;
  const sessionPromise = api.request('/api/session');
  env.render({session: {authenticated: false}}, 'https://other.invalid');
  assert.equal(env.requests().length, 0);
  env.render({session: {authenticated: true, user: {username: 'synthetic-admin'}}});
  assert.equal((await sessionPromise).user.username, 'synthetic-admin');
  assert.equal(env.requests().length, 0, 'Initial session comes from authenticated component arguments.');

  const first = api.request('/api/ledger?branch=x&date=2026-10-16');
  const duplicate = api.request('/api/ledger?branch=x&date=2026-10-16');
  const second = api.request('/api/ledger/actions', {method: 'POST', body: {action: {raw: {patient_name: '합성'}}}});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(env.requests().length, 1, 'A queued mutation cannot replace an in-flight Streamlit component value.');
  const firstId = env.requests()[0].message.value.request_id;
  env.render({response: {request_id: 'unrelated', status: 200, data: {wrong: true}}});
  assert.equal(env.requests().length, 1);
  env.render({response: {request_id: firstId, status: 200, data: {scope: 'synthetic-scope'}}});
  assert.equal((await first).scope, 'synthetic-scope');
  assert.equal((await duplicate).scope, 'synthetic-scope');
  assert.equal(env.requests().length, 2, 'Identical concurrent reads are coalesced.');
  const secondId = env.requests()[1].message.value.request_id;
  env.render({response: {request_id: secondId, status: 409,
    data: {error: {code: 'conflict', message: '합성 충돌', current: {revision: 2}}}}});
  await assert.rejects(second, error => error.status === 409 && error.code === 'conflict' && error.current.revision === 2);

  const backup = api.request('/api/ledger/backup', {blob: true});
  await Promise.resolve(); await Promise.resolve();
  const thirdId = env.requests()[2].message.value.request_id;
  env.render({response: {request_id: thirdId, status: 200, data: {payload: {schema_version: 1}, sha256: 'synthetic'}}});
  assert.equal((await backup).type, 'application/json');
  assert.deepEqual(JSON.parse(await (await backup).text()), {payload: {schema_version: 1}, sha256: 'synthetic'});

  const slow = api.request('/api/ledger?branch=x&date=2026-10-17');
  await Promise.resolve(); await Promise.resolve();
  for (const timer of [...env.timers.values()]) timer();
  await assert.rejects(slow, error => error.status === 503);
  assert.equal(env.window.localStorage, undefined, 'Transport does not require browser storage.');
  console.log('Streamlit full B transport: parent origin, bootstrap, serial commands, read coalescing, errors, backup and timeout passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
