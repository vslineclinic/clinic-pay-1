import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../assets/remember_cookie/index.html', import.meta.url), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const origin = 'https://clinic-pay.example';
const name = '__Host-clinic_pay_remember';
const token = '11111111-1111-4111-8111-111111111111.' + 'a'.repeat(43);

function browser({jar = new Map(), blockRead = false, blockWrite = false} = {}) {
  const messages = [], writes = [], listeners = {};
  const parent = {postMessage(value, destination) {
    assert.equal(destination, origin);
    messages.push(JSON.parse(JSON.stringify(value)));
  }};
  const document = {};
  Object.defineProperty(document, 'cookie', {
    get() {
      if (blockRead) throw new Error('Storage blocked');
      return [...jar].map(([key, value]) => `${key}=${value}`).join('; ');
    },
    set(value) {
      writes.push(value);
      if (blockWrite) return;
      const [pair] = value.split(';');
      const separator = pair.indexOf('=');
      const key = pair.slice(0, separator), body = pair.slice(separator + 1);
      if (value.includes('Max-Age=0;')) jar.delete(key);
      else jar.set(key, body);
    },
  });
  const window = {parent, location: {origin}, addEventListener(type, listener) {listeners[type] = listener;}};
  vm.runInNewContext(source, {window, document});
  const render = (command, overrides = {}) => listeners.message({
    source: parent, origin, data: {type: 'streamlit:render', args: {command}}, ...overrides,
  });
  const values = () => messages.filter(message => message.type === 'streamlit:setComponentValue');
  return {jar, writes, messages, render, values, value: () => values().at(-1)?.value};
}

const first = browser();
assert.equal(first.messages[0].type, 'streamlit:componentReady');
first.render(null, {origin: 'https://foreign.example'});
first.render(null, {source: {}});
assert.equal(first.values().length, 0);
first.render(null);
assert.deepEqual(first.value(), {ready: true, token: '', ack: '', ok: true});
const set = {action: 'set', nonce: 'set-one', token, max_age: 15552000};
first.render(set);
assert.deepEqual(first.value(), {ready: true, token, ack: 'set-one', ok: true});
assert.match(first.writes[0], /; Max-Age=15552000; Path=\/; Secure; SameSite=Strict$/);
assert(!first.writes[0].includes('Domain='));
const deliveries = first.values().length;
first.render(set);
assert.equal(first.writes.length, 1);
assert.equal(first.values().length, deliveries);

const reopened = browser({jar: first.jar});
reopened.render(null);
assert.equal(reopened.value().token, token);
assert.equal(reopened.writes.length, 0);
reopened.render({action: 'clear', nonce: 'logout-one'});
assert.deepEqual(reopened.value(), {ready: true, token: '', ack: 'logout-one', ok: true});
assert.match(reopened.writes[0], /; Max-Age=0; Path=\/; Secure; SameSite=Strict$/);
const loggedOut = browser({jar: reopened.jar});
loggedOut.render(null);
assert.equal(loggedOut.value().token, '');

for (const command of [
  {...set, token: 'unsafe; Domain=foreign.example'},
  {...set, max_age: 15552001},
  {...set, max_age: 0},
  {...set, max_age: 1.5},
]) {
  const invalid = browser();
  invalid.render(command);
  assert.equal(invalid.writes.length, 0);
  assert.equal(invalid.value().ok, false);
}
const blocked = browser({blockWrite: true});
blocked.render(set);
assert.equal(blocked.value().ok, false);
assert.equal(blocked.value().token, '');
const unreadable = browser({blockRead: true});
unreadable.render(null);
assert.deepEqual(unreadable.value(), {ready: true, token: '', ack: '', ok: false});
const malformed = browser({jar: new Map([[name, '%']])});
malformed.render(null);
assert.equal(malformed.value().ok, false);
console.log('Cookie bridge: save, reopen, clear, origin checks, expiry bounds and blocked storage passed');
