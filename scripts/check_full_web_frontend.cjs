const assert = require('node:assert/strict');
const {
  SaveController,
  blank,
  money,
  total,
  MAX_MONEY,
  canVoid,
  visibleRow
} = require('../web_static/core.js');
const fs = require('node:fs'),
  vm = require('node:vm');
function fixture(request) {
  let now = 0,
    sequence = 0,
    id = 0;
  const timers = new Map(),
    calls = [];
  const controller = new SaveController({
    request: async (path, options) => {
      calls.push(options.body);
      return request ? request(options.body) : {
        ack: {
          request_id: options.body.action.request_id,
          attempt: options.body.action.attempt || 0,
          status: 'saved',
          receipt_revision: 1,
          draft_revision: 0
        }
      };
    },
    uuid: () => `00000000-0000-4000-8000-${String(++id).padStart(12, '0')}`,
    clock: () => now,
    setTimer: (fn, delay) => {
      timers.set(++sequence, {
        fn,
        at: now + delay
      });
      return sequence;
    },
    clearTimer: id => timers.delete(id)
  });
  const m = controller.select('scope-a', {
      branch: '인천점',
      date: '2026-10-15'
    }),
    row = m.add({
      ...blank(),
      chart_no: 'TEST',
      patient_name: '가상',
      payments: {
        ...blank().payments,
        카드: '1000'
      }
    });
  async function tick(ms) {
    now += ms;
    for (const [id, t] of [...timers]) if (t.at <= now) {
      timers.delete(id);
      await t.fn();
    }
    await Promise.resolve();
  }
  return {
    controller,
    m,
    row,
    calls,
    tick
  };
}
(async () => {
  assert.equal(money('9,007,199,254,740,993'), 9007199254740993n);
  assert.equal(total({
    ...blank(),
    payments: {
      카드: String(MAX_MONEY)
    }
  }), MAX_MONEY);
  assert.throws(() => money('1,00'));
  assert.throws(() => money('1.5'));
  assert.throws(() => total({
    ...blank(),
    payments: {
      카드: String(MAX_MONEY),
      현금: '1'
    }
  }));
  let f = fixture();
  f.m.edit(f.row, 'note', '첫 글자');
  f.controller.changed('scope-a');
  await f.tick(1199);
  assert.equal(f.calls.length, 0);
  await f.tick(1);
  assert.equal(f.calls.length, 1);
  assert.equal(f.m.rows.get(f.row).status, 'saved');
  f = fixture();
  for (let i = 0; i < 5; i++) {
    f.m.edit(f.row, 'note', String(i));
    f.controller.changed('scope-a');
    await f.tick(1000);
  }
  assert.equal(f.calls.length, 1, 'maxwait prevents endless debounce');
  assert.equal(f.calls[0].action.raw.note, '4');
  f = fixture();
  f.controller.composing('scope-a', f.row, true);
  f.m.edit(f.row, 'patient_name', 'ㅎ');
  f.controller.changed('scope-a');
  await f.tick(5000);
  assert.equal(f.calls.length, 0, 'no writes mid Korean composition');
  f.m.edit(f.row, 'patient_name', '홍');
  f.controller.composing('scope-a', f.row, false);
  await f.tick(1200);
  assert.equal(f.calls[0].action.raw.patient_name, '홍');
  let resolve;
  f = fixture(body => new Promise(r => resolve = () => r({
    ack: {
      request_id: body.action.request_id,
      status: 'saved',
      receipt_revision: 1,
      draft_revision: 0
    }
  })));
  f.m.edit(f.row, 'note', 'first');
  f.controller.changed('scope-a');
  const first = f.tick(1200);
  await Promise.resolve();
  f.m.edit(f.row, 'note', 'later');
  f.controller.changed('scope-a');
  resolve();
  await first;
  assert.equal(f.m.rows.get(f.row).raw.note, 'later');
  assert.equal(f.m.rows.get(f.row).dirty, true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].action.raw.note, 'first', 'inflight command remains immutable');
  let attempts = 0;
  f = fixture(body => {
    if (++attempts === 1) throw Error('response lost');
    return {
      ack: {
        request_id: body.action.request_id,
        attempt: body.action.attempt,
        status: 'saved',
        receipt_revision: 1,
        draft_revision: 0
      }
    };
  });
  f.m.edit(f.row, 'note', 'original');
  f.controller.changed('scope-a');
  await f.tick(1200);
  const original = f.calls[0].action.request_id;
  f.m.edit(f.row, 'note', 'next');
  f.controller.retry('scope-a', f.row);
  await f.tick(0);
  assert.equal(f.calls[1].action.request_id, original);
  assert.equal(f.calls[1].action.raw.note, 'original');
  assert.equal(f.calls[1].action.attempt, 1);
  assert.equal(f.calls[2].action.raw.note, 'next');
  f = fixture(body => ({
    ack: {
      request_id: body.action.request_id,
      status: 'conflict',
      current: {
        revision: 2,
        chart_no: 'TEST',
        patient_name: 'other',
        payments: {
          카드: '2000'
        }
      },
      message: 'conflict'
    }
  }));
  f.m.edit(f.row, 'note', 'mine');
  f.controller.changed('scope-a');
  await f.tick(1200);
  assert.equal(f.m.rows.get(f.row).status, 'conflict');
  f.controller.flush('scope-a');
  await f.tick(0);
  assert.equal(f.calls.length, 1, 'conflict never force-overwrites');
  f = fixture();
  f.m.edit(f.row, 'note', 'old day');
  f.controller.changed('scope-a');
  const b = f.controller.select('scope-b', {
      branch: '인천점',
      date: '2026-10-16'
    }),
    brow = b.add(blank());
  b.edit(brow, 'note', 'new day');
  f.controller.changed('scope-b');
  await f.tick(1200);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].date, '2026-10-15');
  assert.equal(f.calls[1].date, '2026-10-16');
  f = fixture(() => ({
    ack: {
      request_id: 'wrong-id',
      status: 'saved'
    }
  }));
  f.m.edit(f.row, 'note', 'wrong ack');
  f.controller.changed('scope-a');
  await f.tick(1200);
  assert.equal(f.m.rows.get(f.row).status, 'error');
  assert.equal(f.m.rows.get(f.row).failed.raw.note, 'wrong ack');
  f = fixture();
  f.m.edit(f.row, 'note', 'pending');
  f.controller.changed('scope-a');
  f.controller.stop();
  await f.tick(9000);
  assert.equal(f.calls.length, 0);
  assert.equal(f.controller.registry.models.size, 0);
  const transport = {
    sentinel: true
  };
  vm.runInNewContext(fs.readFileSync('web_static/api.js', 'utf8'), {
    window: {
      LedgerTransport: transport
    }
  });
  assert.equal(transport.sentinel, true);
  const index = fs.readFileSync('web_static/index.html', 'utf8'),
    preview = fs.readFileSync('web_static/preview.html', 'utf8');
  assert.ok(index.includes('bridge-api.js'));
  assert.ok(!index.includes('demo-api.js'));
  assert.ok(preview.includes('demo-api.js'));
  assert.ok(!preview.includes('bridge-api.js'));
  const app = fs.readFileSync('web_static/app.js', 'utf8');
  assert.ok(!app.includes("m.resolve("), 'conflict resolution reloads authoritative state instead of resaving it');
  assert.ok(app.includes('C.canVoid'), 'UI uses tested void guard');
  f = fixture();
  const draftRow = f.m.rows.get(f.row);
  draftRow.receipt_revision = 1;
  draftRow.draft_revision = 1;
  assert.equal(canVoid(draftRow, f.m), false, 'persisted invalid draft cannot be voided');
  draftRow.draft_revision = 0;
  assert.equal(canVoid(draftRow, f.m), true);
  draftRow.conflict = {};
  draftRow.draft_revision = 1;
  f.controller.replaceFromServer('scope-a', f.row, []);
  assert.equal(f.m.rows.has(f.row), false, 'deleted server draft removes stale local conflict');
  assert.equal(f.controller.pending(), false);
  f.controller.replaceFromServer('scope-a', f.row, [{
    id: f.row,
    raw: blank(),
    receipt_revision: 2,
    draft_revision: 0,
    voided: true
  }]);
  assert.equal(f.m.rows.get(f.row).voided, true, 'latest voided state is retained');
  assert.equal(canVoid(f.m.rows.get(f.row), f.m), false);
  assert.equal(visibleRow(f.m.rows.get(f.row)), false);
  f.m.rows.get(f.row).draft_revision = 1;
  assert.equal(visibleRow(f.m.rows.get(f.row)), true, 'voided receipt with remaining draft stays visible for cleanup');
  assert.equal(canVoid(f.m.rows.get(f.row), f.m), false);
  for (const flag of ['dirty', 'failed', 'conflict']) {
    const r = {
      receipt_revision: 1,
      draft_revision: 0,
      [flag]: true
    };
    assert.equal(canVoid(r, {
      active: null
    }), false);
  }
  assert.equal(canVoid({
    receipt_revision: 1
  }, {
    active: {}
  }), false);
  assert.equal(canVoid({
    receipt_revision: 1
  }, {
    active: null
  }, true), false);
  assert.ok(!app.includes('localStorage'));
  assert.ok(!app.includes('sessionStorage'));
  assert.ok(app.includes('beforeunload'));
  const demoWindow = {
    LedgerCore: require('../web_static/core.js')
  };
  vm.runInNewContext(fs.readFileSync('web_static/demo-api.js', 'utf8'), {
    window: demoWindow,
    crypto: require('node:crypto').webcrypto,
    URL,
    Blob,
    location: {
      href: 'https://preview.invalid/'
    }
  });
  const demo = demoWindow.LedgerTransport;
  const initial = await demo.request('/api/ledger?branch=' + encodeURIComponent('데모 지점') + '&date=2026-10-15');
  assert.equal(initial.rows.length, 11);
  const action = {
    scope: initial.scope,
    request_id: require('node:crypto').randomUUID(),
    id: require('node:crypto').randomUUID(),
    action: 'save',
    raw: {
      ...blank(),
      chart_no: 'TEST64',
      patient_name: '가상',
      payments: {
        ...blank().payments,
        카드: '9007199254740993'
      }
    },
    receipt_revision: 0,
    draft_revision: 0
  };
  const body = {
    branch: '데모 지점',
    date: '2026-10-15',
    scope: initial.scope,
    action
  };
  const saved = await demo.request('/api/ledger/actions', {
    method: 'POST',
    body
  });
  assert.equal(saved.ack.status, 'saved');
  const duplicate = await demo.request('/api/ledger/actions', {
    method: 'POST',
    body: {
      ...body,
      action: {
        ...action,
        attempt: 1
      }
    }
  });
  assert.equal(duplicate.ack.receipt_revision, 1);
  const latest = await demo.request('/api/ledger?branch=' + encodeURIComponent('데모 지점') + '&date=2026-10-15');
  assert.equal(latest.rows.find(r => r.id === action.id).raw.payments.카드, '9007199254740993');
  await demo.request('/api/ledger/close', {
    method: 'POST',
    body: {
      branch: '데모 지점',
      date: '2026-10-15',
      expected_revision: latest.day.revision
    }
  });
  const snaps = await demo.request('/api/ledger/snapshots?branch=' + encodeURIComponent('데모 지점') + '&date=2026-10-15');
  assert.equal(typeof snaps.snapshots[0].download_json, 'string');
  console.log('Full web frontend: precision, debounce, maxwait, IME, immutable retries, conflicts, dates, ACK validation, logout, transport isolation passed.');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
