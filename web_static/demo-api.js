/* Synthetic preview only. Production index never loads this adapter. */
(function (root) {
  'use strict';

  const C = root.LedgerCore,
    copy = v => JSON.parse(JSON.stringify(v)),
    today = '2026-10-15',
    days = new Map(),
    events = new Map(),
    snapshots = new Map(),
    requests = new Map();
  let authenticated = true;
  const key = (branch, date) => branch + '|' + date;
  function day(branch, date) {
    const k = key(branch, date);
    if (!days.has(k)) days.set(k, {
      branch,
      business_date: date,
      state: 'open',
      revision: 0,
      receipts: [],
      drafts: []
    });
    return days.get(k);
  }
  function create(chart, name, payments, extra = {}) {
    return {
      id: crypto.randomUUID(),
      revision: 1,
      ...C.blank(),
      chart_no: chart,
      patient_name: name,
      payments: {
        ...C.blank().payments,
        ...payments
      },
      voided: false,
      updated_by: '체험 관리자',
      ...extra
    };
  }
  const sample = day('데모 지점', today);
  sample.receipts = [create('DEMO001', '예시환자 A', {
    '카드': '110000'
  }), create('DEMO002', '예시환자 B', {
    '카드': '55000'
  }), create('DEMO003', '예시환자 C', {
    '이체': '88000'
  }), create('DEMO004', '예시환자 D', {
    '카드': '200000'
  }, {
    note: '카드 2건 분할 결제'
  }), create('DEMO005', '예시환자 E', {
    '카드': '150000'
  }), create('DEMO006', '예시환자 F', {
    '현금': '77000'
  }), create('DEMO007', '예시환자 G', {
    '여신티켓': '99000'
  }, {
    yeoshin_used: true
  }), create('DEMO008', '예시환자 H', {
    '강남언니': '132000'
  }, {
    gangnamunni_used: true
  }), create('DEMO009', '예시환자 I', {
    '제로페이': '44000'
  }), create('DEMO010', '예시환자 J', {
    '카드': '66000',
    '현금': '22000'
  })];
  sample.receipts.push(create('DEMO005', '예시환자 E', {
    '카드': '50000'
  }, {
    kind: 'refund',
    refund_of: sample.receipts[4].id,
    note: '일부 시술 취소'
  }));
  sample.revision = sample.receipts.length;
  for (const date of ['2026-10-13', '2026-10-14']) {
    const d = day('데모 지점', date);
    d.receipts = [create('DEMO011', '이전일 예시', {
      '카드': date.endsWith('13') ? '176000' : '220000'
    })];
    d.revision = 1;
  }
  function session() {
    return {
      authenticated,
      csrf_token: 'synthetic-preview-only',
      user: authenticated ? {
        username: '체험 관리자',
        role: 'admin',
        branch: 'all'
      } : null,
      branches: ['데모 지점'],
      channels: C.CHANNELS,
      today
    };
  }
  function summary(d) {
    let gross = 0n,
      refund = 0n,
      receipt_count = 0,
      refund_count = 0;
    for (const r of d.receipts.filter(r => !r.voided)) {
      if (r.kind === 'refund') {
        refund += C.total(r);
        refund_count++;
      } else {
        gross += C.total(r);
        receipt_count++;
      }
    }
    return {
      gross: String(gross),
      refund: String(refund),
      net: String(gross - refund),
      receipt_count,
      refund_count
    };
  }
  function workspace(branch, date) {
    const d = day(branch, date),
      rows = d.receipts.map(r => ({
        id: r.id,
        raw: Object.fromEntries(Object.keys(C.blank()).map(k => [k, copy(r[k])])),
        receipt_revision: r.revision,
        draft_revision: 0,
        voided: r.voided,
        updated_by: r.updated_by
      }));
    for (const draft of d.drafts) {
      const row = rows.find(r => r.id === draft.id);
      if (row) Object.assign(row, {
        raw: copy(draft.raw),
        draft_revision: draft.revision,
        receipt_revision: draft.base
      });else rows.push({
        id: draft.id,
        raw: copy(draft.raw),
        receipt_revision: draft.base,
        draft_revision: draft.revision,
        voided: false
      });
    }
    return {
      day: copy(d),
      rows,
      summary: summary(d),
      scope: key(branch, date),
      pending_count: d.drafts.length
    };
  }
  function fail(message, status = 400) {
    throw Object.assign(Error(message), {
      status
    });
  }
  function record(d, action, id) {
    const k = key(d.branch, d.business_date);
    events.set(k, [{
      action,
      receipt_id: id,
      actor: '체험 관리자',
      created_at: new Date().toISOString()
    }, ...(events.get(k) || [])]);
    d.revision++;
    d.updated_at = new Date().toISOString();
  }
  function command(body) {
    const {
        branch,
        date,
        scope,
        action: a
      } = body,
      d = day(branch, date),
      k = key(branch, date);
    if (scope !== k || a.scope !== k) fail('영업일이 일치하지 않습니다.');
    const fingerprint = JSON.stringify({
        ...a,
        attempt: 0
      }),
      prior = requests.get(a.request_id);
    if (prior) {
      if (prior.fingerprint !== fingerprint) fail('같은 요청 식별자의 내용이 다릅니다.');
      return {
        ack: {
          ...copy(prior.ack),
          attempt: a.attempt || 0
        }
      };
    }
    const ack = {
      id: a.id,
      request_id: a.request_id,
      attempt: a.attempt || 0
    };
    if (d.state === 'closed') return {
      ack: {
        ...ack,
        status: 'error',
        message: '마감된 원장입니다.'
      }
    };
    let r = d.receipts.find(r => r.id === a.id),
      draft = d.drafts.find(r => r.id === a.id);
    if ((r?.revision || 0) !== a.receipt_revision) return {
      ack: {
        ...ack,
        status: 'conflict',
        current: copy(r || {}),
        message: '다른 입력에서 이 기록을 수정했습니다. 서버값을 검토하세요.'
      }
    };
    if ((draft?.revision || 0) !== a.draft_revision) return {
      ack: {
        ...ack,
        status: 'conflict',
        current: draft ? {
          values: copy(draft.raw),
          revision: draft.revision
        } : {},
        message: '초안 개정이 변경되었습니다.'
      }
    };
    let result;
    if (a.action === 'discard') {
      d.drafts = d.drafts.filter(r => r.id !== a.id);
      record(d, '초안 삭제', a.id);
      result = {
        ...ack,
        status: 'discarded',
        receipt_revision: r?.revision || 0,
        draft_revision: 0
      };
    } else if (a.action === 'void') {
      if (!r || !a.raw._void_reason?.trim()) fail('취소 사유를 입력하세요.');
      if (d.receipts.some(x => !x.voided && x.refund_of === r.id)) fail('환불이 연결된 원수납은 취소할 수 없습니다.');
      r.voided = true;
      r.revision++;
      record(d, '수납 취소', a.id);
      result = {
        ...ack,
        status: 'saved',
        receipt_revision: r.revision,
        draft_revision: 0,
        voided: true
      };
    } else {
      let message = '';
      try {
        if (!a.raw.chart_no.trim() || !a.raw.patient_name.trim()) throw Error('차트번호와 환자명을 입력해 주세요.');
        if (!C.total(a.raw)) throw Error('결제 금액이 없는 행은 초안으로 보관합니다.');
        if (a.raw.kind === 'refund') {
          const original = [...days.values()].flatMap(x => x.receipts).find(x => x.id === a.raw.refund_of && !x.voided && x.kind === 'receipt');
          if (!original) throw Error('환불할 원수납을 선택하세요.');
          if (original.chart_no !== a.raw.chart_no || original.patient_name !== a.raw.patient_name) throw Error('원수납의 차트번호·환자명을 확인해 주세요.');
          const refunds = [...days.values()].flatMap(x => x.receipts).filter(x => x.id !== a.id && !x.voided && x.refund_of === original.id);
          for (const channel of C.CHANNELS) if (refunds.reduce((n, x) => n + C.money(x.payments[channel]), C.money(a.raw.payments[channel])) > C.money(original.payments[channel])) throw Error(channel + ' 누적 환불이 원수납 금액을 초과합니다.');
        }
      } catch (error) {
        message = error.message;
      }
      if (message) {
        draft = {
          id: a.id,
          raw: copy(a.raw),
          revision: (draft?.revision || 0) + 1,
          base: a.receipt_revision
        };
        d.drafts = d.drafts.filter(r => r.id !== a.id);
        d.drafts.push(draft);
        record(d, '초안 저장', a.id);
        result = {
          ...ack,
          status: 'draft',
          receipt_revision: r?.revision || 0,
          draft_revision: draft.revision,
          message
        };
      } else {
        const value = {
          ...copy(a.raw),
          payments: Object.fromEntries(C.CHANNELS.map(k => [k, String(C.money(a.raw.payments[k]))])),
          id: a.id,
          revision: (r?.revision || 0) + 1,
          voided: false,
          updated_by: '체험 관리자'
        };
        if (r) Object.assign(r, value);else d.receipts.push(value);
        d.drafts = d.drafts.filter(r => r.id !== a.id);
        record(d, r ? '수납 수정' : '수납 등록', a.id);
        result = {
          ...ack,
          status: 'saved',
          receipt_revision: value.revision,
          draft_revision: 0
        };
      }
    }
    requests.set(a.request_id, {
      fingerprint,
      ack: copy(result)
    });
    return {
      ack: result
    };
  }
  root.LedgerTransport = {
    mode: 'demo',
    async request(path, {
      method = 'GET',
      body,
      blob = false
    } = {}) {
      const url = new URL(path, location.href),
        branch = url.searchParams.get('branch') || body?.branch,
        date = url.searchParams.get('date') || body?.date;
      if (url.pathname === '/api/session') {
        if (method === 'DELETE') authenticated = false;else if (method === 'POST') authenticated = true;
        return session();
      }
      if (!authenticated) fail('체험을 다시 시작하려면 로그인 버튼을 눌러 주세요.', 401);
      if (url.pathname === '/api/ledger') return workspace(branch, date);
      if (url.pathname === '/api/ledger/actions') return command(body);
      if (url.pathname === '/api/ledger/days') return {
        days: copy([...days.values()].filter(d => d.branch === branch && d.business_date >= url.searchParams.get('start') && d.business_date <= url.searchParams.get('end') && (d.revision || d.receipts.length)).sort((a, b) => b.business_date.localeCompare(a.business_date)))
      };
      if (url.pathname === '/api/ledger/events') return {
        events: copy(events.get(key(branch, date)) || [])
      };
      if (url.pathname === '/api/ledger/snapshots') return {
        snapshots: (snapshots.get(key(branch, date)) || []).map(item => ({
          ...copy(item),
          download_json: JSON.stringify(item)
        }))
      };
      if (url.pathname === '/api/ledger/backup' && blob) return new Blob([JSON.stringify({
        format: 'synthetic-preview-only',
        notice: '가상 데이터 · 실제 서버 백업이 아닙니다.',
        days: [...days.values()]
      }, null, 2)], {
        type: 'application/json'
      });
      if (['/api/ledger/close', '/api/ledger/reopen'].includes(url.pathname)) {
        const d = day(branch, date),
          k = key(branch, date);
        if (d.revision !== body.expected_revision) fail('원장 개정이 바뀌었습니다. 새로고침 후 다시 검토하세요.', 409);
        if (url.pathname.endsWith('/close')) {
          if (d.drafts.length) fail('미완료 초안이 남아 있습니다.');
          d.state = 'closed';
          const list = snapshots.get(k) || [];
          list.push({
            id: crypto.randomUUID(),
            close_sequence: list.length + 1,
            created_at: new Date().toISOString(),
            snapshot: copy(d)
          });
          snapshots.set(k, list);
          record(d, '원장 마감', '');
        } else {
          if (!body.reason?.trim()) fail('재개방 사유를 입력하세요.');
          d.state = 'open';
          record(d, '원장 재개방', '');
        }
        return {
          result: copy(d)
        };
      }
      fail('체험 화면에서 지원하지 않는 요청입니다.', 404);
    }
  };
})(window);
