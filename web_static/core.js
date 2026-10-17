/* Browser-only edits. Immutable requests leave the page only after the debounce. */
(function (root) {
  'use strict';

  const {
    EditorRegistry
  } = typeof module !== 'undefined' ? require('./state.js') : root.LedgerEditor;
  const CHANNELS = ['카드', '현금', '이체', '여신티켓', '강남언니', '나만의닥터', '제로페이', '기타지역화폐', '알리페이', '위챗페이', '카카오페이', '간편결제', '바비톡', '닥터나우'];
  const MAX_MONEY = 9223372036854775807n;
  function money(value) {
    const text = String(value ?? '').trim();
    if (!text) return 0n;
    if (!/^(\d+|\d{1,3}(,\d{3})+)$/.test(text)) throw Error('금액은 0 이상의 정수 원으로 입력하세요.');
    const n = BigInt(text.replaceAll(',', ''));
    if (n > MAX_MONEY) throw Error('금액이 저장 범위를 초과합니다.');
    return n;
  }
  function total(raw) {
    const n = CHANNELS.reduce((sum, key) => sum + money(raw.payments?.[key]), 0n);
    if (n > MAX_MONEY) throw Error('금액 합계가 저장 범위를 초과합니다.');
    return n;
  }
  function blank(kind = 'receipt') {
    return {
      chart_no: '',
      patient_name: '',
      note: '',
      kind,
      refund_of: null,
      yeoshin_used: false,
      gangnamunni_used: false,
      payments: Object.fromEntries(CHANNELS.map(k => [k, '']))
    };
  }
  function won(value) {
    try {
      return BigInt(value).toLocaleString('ko-KR');
    } catch {
      return '확인 필요';
    }
  }
  function visibleRow(row) {
    return !row.voided || !!row.draft_revision;
  }
  function canVoid(row, model, closed = false) {
    return !!row.receipt_revision && !row.voided && !model.active && !row.dirty && !row.failed && !row.conflict && !row.draft_revision && !closed;
  }
  class SaveController {
    constructor({
      request,
      onChange = () => {},
      onSaved = () => {},
      onUnauthorized = () => {},
      uuid = () => crypto.randomUUID(),
      clock = () => Date.now(),
      setTimer = (...args) => root.setTimeout(...args),
      clearTimer = id => root.clearTimeout(id),
      delay = 1200,
      maxWait = 5000
    }) {
      Object.assign(this, {
        request,
        onChange,
        onSaved,
        onUnauthorized,
        clock,
        setTimer,
        clearTimer,
        delay,
        maxWait
      });
      this.registry = new EditorRegistry(uuid);
      this.contexts = new Map();
      this.due = new Map();
      this.generation = 0;
      this.running = false;
      this.stopped = false;
    }
    select(scope, context) {
      this.contexts.set(scope, {
        ...context
      });
      return this.registry.select(scope);
    }
    changed(scope) {
      this.generation++;
      const now = this.clock(),
        previous = this.due.get(scope);
      this.due.set(scope, {
        first: previous?.first ?? now,
        at: Math.min(now + this.delay, (previous?.first ?? now) + this.maxWait)
      });
      this.onChange();
      this.schedule();
    }
    schedule() {
      if (this.stopped) return;
      this.clearTimer(this.timer);
      if (!this.due.size) return;
      const at = Math.min(...[...this.due.values()].map(v => v.at));
      this.timer = this.setTimer(() => this.drain(), Math.max(0, at - this.clock()));
    }
    flush(scope) {
      if (this.stopped) return;
      this.due.set(scope, {
        first: this.clock(),
        at: this.clock()
      });
      this.schedule();
    }
    async drain() {
      if (this.running || this.stopped) return;
      this.running = true;
      try {
        for (const [scope, due] of [...this.due]) {
          if (due.at > this.clock()) continue;
          const model = this.registry.models.get(scope);
          this.due.delete(scope);
          let action = model.next();
          while (action && !this.stopped) {
            this.onChange();
            try {
              const response = await this.request('/api/ledger/actions', {
                method: 'POST',
                body: {
                  ...this.contexts.get(scope),
                  scope,
                  action
                }
              });
              if (this.stopped) break;
              if (response.ack?.request_id !== action.request_id || (response.ack.attempt || 0) !== (action.attempt || 0) || !['saved', 'draft', 'discarded', 'conflict', 'error'].includes(response.ack.status)) throw Error('저장 응답 식별자가 맞지 않습니다. 동일 요청으로 다시 시도하세요.');
              model.ack(response.ack);
              this.generation++;
              this.onChange();
              if (['saved', 'draft', 'discarded'].includes(response.ack.status)) this.onSaved(scope, response.ack);
            } catch (error) {
              if (this.stopped) break;
              model.ack({
                request_id: action.request_id,
                attempt: action.attempt || 0,
                status: 'error',
                message: error.message || '저장 응답을 확인하지 못했습니다. 재시도해 주세요.'
              });
              this.onChange();
              if (error.status === 401) {
                this.onUnauthorized();
                break;
              }
            }
            // A new edit while a request was in flight retains its own debounce deadline.
            if (this.due.has(scope) && this.due.get(scope).at > this.clock()) break;
            action = model.next();
          }
        }
      } finally {
        this.running = false;
        this.schedule();
      }
    }
    retry(scope, id) {
      this.registry.models.get(scope).retry(id);
      this.flush(scope);
    }
    composing(scope, id, active) {
      const model = this.registry.models.get(scope);
      if (active) model.composing.add(id);else {
        model.composing.delete(id);
        if (model.rows.get(id)?.dirty) this.changed(scope);
      }
    }
    pending() {
      return this.registry.pending();
    }
    replaceFromServer(scope, id, rows) {
      const model = this.registry.models.get(scope);
      if (!model || model.active?.id === id) throw Error("저장 중인 기록은 다시 불러올 수 없습니다.");
      model.rows.delete(id);
      model.sync(rows);
      this.generation++;
      this.onChange();
    }
    stop() {
      this.stopped = true;
      this.clearTimer(this.timer);
      this.due.clear();
      this.contexts.clear();
      this.registry.models.clear();
    }
  }
  const api = {
    CHANNELS,
    MAX_MONEY,
    money,
    total,
    blank,
    won,
    SaveController,
    canVoid,
    visibleRow
  };
  if (typeof module !== 'undefined') module.exports = api;
  root.LedgerCore = api;
})(typeof window !== 'undefined' ? window : globalThis);
