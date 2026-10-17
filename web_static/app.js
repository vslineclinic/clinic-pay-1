/* Full concept layout; local input and rendering are independent of the server. */
(function () {
  'use strict';

  const C = window.LedgerCore,
    T = window.LedgerTransport,
    $ = s => document.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[c]);
  const labels = {
    ledger: '수납 원장',
    workbench: '대조 작업대',
    closing: '일일 마감',
    history: '기간별 기록',
    'reconciliation-history': '정산 기록',
    analysis: '오차 분석',
    activity: '변경 이력'
  };
  const statusLabels = {
    saved: '저장완료',
    draft: '초안 저장',
    editing: '저장 대기',
    saving: '저장 중',
    new: '입력 전',
    error: '저장 확인 필요',
    conflict: '동시 수정 확인'
  };
  let session,
    controller,
    branch = '',
    date = '',
    tab = 'ledger',
    scope = '',
    workspace = null,
    loadSequence = 0,
    query = '',
    toastTimer,
    pollTimer,
    editing = null,
    rangeFrom = '',
    rangeTo = '',
    loadError = '',
    lastSaveMs = null,
    extraSequence = 0;
  const models = () => controller?.registry.models;
  const model = () => models()?.get(scope);
  const rows = () => [...(model()?.rows.values() || [])].filter(C.visibleRow);
  const params = (extra = {}) => new URLSearchParams({
    branch,
    date,
    ...extra
  }).toString();
  function toast(message) {
    $('#toast').textContent = message;
    $('#toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 4000);
  }
  function heading(eyebrow, title, desc, actions = '') {
    return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${desc}</p></div><div class="heading-actions">${actions}</div></div>`;
  }
  function stat(label, value, unit, foot, icon) {
    return `<div class="stat-card"><div class="stat-label">${label}<span class="stat-symbol">${icon}</span></div><div class="stat-number">${value}<small>${unit}</small></div><div class="stat-bottom"><span class="mini-dot"></span>${foot}</div></div>`;
  }
  function statCards() {
    const s = workspace?.summary || {
      gross: '0',
      refund: '0',
      net: '0',
      receipt_count: 0,
      refund_count: 0
    };
    return `<div class="stats-grid">${stat('총수납', C.won(s.gross), '원', `수납 영수증 ${s.receipt_count}건`, '＋')}${stat('총환불', C.won(s.refund), '원', `환불 영수증 ${s.refund_count}건 · 별도 기록`, '−')}${stat('순수납', C.won(s.net), '원', '서버 저장 수납 − 환불', '▤')}${stat('기록 상태', workspace?.day.state === 'closed' ? '마감' : '입력 중', '', `전체 미완료 초안 ${workspace?.pending_count || 0}건`, '◷')}</div>`;
  }
  function badge(row) {
    return `<span class="badge ${esc(row.status)}">${row.voided ? '취소 기록의 초안' : statusLabels[row.status] || '확인 필요'}</span>`;
  }
  function rowAmount(row) {
    try {
      return C.won(C.total(row.raw));
    } catch {
      return '확인 필요';
    }
  }
  function cellAmount(raw, key) {
    try {
      const value = C.money(raw.payments?.[key]);
      return value === 0n ? '<span class="ghost-text">—</span>' : C.won(value);
    } catch {
      return '<span class="inline-error">확인</span>';
    }
  }
  function isClosed() {
    return workspace?.day.state === 'closed';
  }
  function allPending() {
    return controller?.pending();
  }
  function updateStatus() {
    if (!controller) return;
    const all = [...models().values()].flatMap(m => [...m.rows.values()]);
    const failed = all.filter(r => r.failed || r.conflict),
      pending = all.filter(r => r.dirty || r.status === 'saving');
    const text = failed.length ? `확인할 입력 ${failed.length}건` : pending.length ? `자동저장 ${pending.length}건 대기` : lastSaveMs !== null ? `저장완료 · ${lastSaveMs}ms` : '저장된 원장';
    const el = $('#save-indicator');
    el.classList.toggle('save-warning', !!failed.length);
    el.classList.toggle('save-pending', !!pending.length);
    el.innerHTML = `<i></i>${esc(text)}`;
    el.title = lastSaveMs === null ? '저장 응답을 받은 기록만 완료로 표시합니다.' : `마지막 저장 요청부터 응답까지 ${lastSaveMs}ms · 1.2초 입력 대기 시간 제외`;
    if (editing) updateEditorStatus();
  }
  function onLocalChange() {
    updateStatus();
    if (tab === 'ledger' && !$('#editor-dialog').open) renderTables();
  }
  function createController() {
    controller = new C.SaveController({
      request: async (path, options) => {
        const start = performance.now();
        const response = await T.request(path, options);
        if (['saved', 'draft', 'discarded'].includes(response.ack?.status)) lastSaveMs = Math.round(performance.now() - start);
        return response;
      },
      onChange: onLocalChange,
      onSaved: s => {
        if (s === scope) refreshWorkspace(false);
      },
      onUnauthorized: () => expireSession()
    });
  }
  async function bootstrap() {
    try {
      session = await T.request('/api/session');
      if (session.authenticated) await enterApp();else showLogin();
    } catch (error) {
      showLogin(error.message);
    }
  }
  function showLogin(error = '') {
    clearTimeout(pollTimer);
    $('#app-shell').hidden = true;
    $('#login-view').hidden = false;
    $('#login-submit').disabled = false;
    $('#login-submit').textContent = '로그인';
    $('#login-error').textContent = error;
  }
  function expireSession() {
    controller?.stop();
    workspace = null;
    scope = '';
    editing = null;
    $('#editor-dialog').close();
    $('#notice-dialog').close();
    $('#main-content').replaceChildren();
    showLogin('로그인이 만료되었거나 권한이 변경되었습니다. 다시 로그인해 주세요.');
    bootstrap();
  }
  async function enterApp() {
    controller?.stop();
    createController();
    $('#login-view').hidden = true;
    $('#app-shell').hidden = false;
    $('#password').value = '';
    $('#profile-name').textContent = session.user.username;
    $('#branch-select').innerHTML = session.branches.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    branch = session.branches.includes(session.workspace?.branch) ? session.workspace.branch : session.branches.includes(branch) ? branch : session.branches[0];
    date = session.workspace?.business_date || date || session.today;
    rangeFrom = date.slice(0, 8) + '01';
    rangeTo = date;
    $('#branch-select').value = branch;
    $('#workspace-name').textContent = branch;
    $('#business-date').value = date;
    const hash = location.hash.slice(1);
    tab = labels[session.tab] ? session.tab : labels[hash] ? hash : 'ledger';
    await refreshWorkspace(true);
    schedulePoll();
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!controller || controller.stopped) return;
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await refreshWorkspace(false);
      schedulePoll();
    }, 5000);
  }
  async function refreshWorkspace(showLoading = false) {
    const target = {
        branch,
        date
      },
      sequence = ++loadSequence,
      generation = controller?.generation;
    if (showLoading) {
      workspace = null;
      scope = '';
      $('#main-content').innerHTML = '<div class="empty-state">원장을 불러오는 중입니다.</div>';
    }
    try {
      const data = await T.request('/api/ledger?' + new URLSearchParams(target));
      if (sequence !== loadSequence || target.branch !== branch || target.date !== date || controller?.stopped) return;
      if (generation !== controller.generation && !showLoading) return;
      const oldScope = scope;
      scope = data.scope;
      const current = controller.select(scope, target);
      current.sync(data.rows, editing?.scope === scope ? new Set([editing.id]) : new Set());
      workspace = data;
      loadError = '';
      $('#business-date').value = date;
      $('#selected-date-note').textContent = date + ' · 한국 영업일';
      $('#workspace-name').textContent = branch;
      $('#date-save-note').textContent = isClosed() ? '마감된 원장 · 수정 잠금' : '입력 1.2초 뒤 자동저장';
      if (showLoading || oldScope !== scope) render();else {
        if (tab === 'ledger') {
          const focused = document.activeElement?.id;
          if (focused !== 'ledger-search') renderTables();
          $('#ledger-stats').innerHTML = statCards();
        } else if (tab === 'closing' && !$('#notice-dialog').open) render();
      }
      updateStatus();
    } catch (error) {
      if (sequence !== loadSequence) return;
      if (error.status === 401) {
        expireSession();
        return;
      }
      loadError = error.message;
      if (showLoading) {
        $('#main-content').innerHTML = `<div class="ledger-error">${esc(error.message)}</div><button class="btn" id="retry-load">다시 불러오기</button>`;
        $('#retry-load')?.addEventListener('click', () => refreshWorkspace(true));
      } else toast(error.message);
    }
  }
  function render() {
    if (!workspace) return;
    document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    $('#breadcrumb-current').textContent = labels[tab];
    $('#main-content').innerHTML = (loadError ? `<div class="ledger-error">${esc(loadError)}</div>` : '') + (tab === 'ledger' ? ledgerView() : tab === 'closing' ? closingView() : tab === 'history' ? historyView() : tab === 'activity' ? activityView() : engineView());
    bindMain();
    if (tab === 'history') loadHistory();
    if (tab === 'activity') loadActivity();
    updateStatus();
  }
  function ledgerView() {
    return heading('RECEIPT LEDGER', '수납 원장', '입력 흐름은 가볍게, 수납과 환불 기록은 정확하게 관리합니다.', `<button class="btn" id="new-refund" ${isClosed() ? 'disabled' : ''}>− 환불 등록</button><button class="btn primary" id="new-receipt" ${isClosed() ? 'disabled' : ''}>＋ 수납 등록</button>`) + `<div class="info-banner">ⓘ <span>${isClosed() ? '마감된 원장입니다. 일일 마감에서 사유를 남겨 재개방할 수 있습니다.' : '입력은 즉시 반영하고 1.2초 후 자동저장합니다. 여신·강언 사용은 표에서 바로 체크하세요.'}</span></div><div id="ledger-stats">${statCards()}</div><section class="panel"><div class="panel-head"><div><div class="panel-title">수납 내역</div><div class="panel-subtitle">저장완료와 미완료 초안을 구분하여 표시합니다. 합계는 서버 저장 기록 기준입니다.</div></div><span class="badge review">14개 결제수단</span></div><div class="search-row"><div class="search-wrap"><span>⌕</span><input id="ledger-search" aria-label="원장 검색" placeholder="환자명, 차트번호, 메모 검색" value="${esc(query)}"></div></div><div id="ledger-table"></div></section><section class="panel stack-gap"><div class="panel-head"><div><div class="panel-title">환불 내역</div><div class="panel-subtitle">원수납을 선택하여 환불을 연결합니다. 다른 날짜의 원수납도 조회할 수 있습니다.</div></div></div><div id="refund-table"></div></section>`;
  }
  function usage(row, field, label) {
    return `<td class="usage-cell"><label class="usage-toggle"><input type="checkbox" data-usage="${field}" data-id="${row.id}" aria-label="${esc(row.raw.patient_name)} ${esc(row.raw.chart_no)} ${label}" ${row.raw[field] ? 'checked' : ''} ${isClosed() || row.voided || row.conflict || row.failed ? 'disabled' : ''}><span>사용</span></label></td>`;
  }
  function renderTables() {
    if (!$('#ledger-table') || !model()) return;
    const list = rows().filter(r => `${r.raw.patient_name} ${r.raw.chart_no} ${r.raw.note}`.toLowerCase().includes(query.toLowerCase())),
      sales = list.filter(r => r.raw.kind !== 'refund'),
      refunds = list.filter(r => r.raw.kind === 'refund');
    $('#ledger-table').innerHTML = `<div class="table-scroll"><table class="reconcile-table wide-table ledger-sticky"><thead><tr><th>순서</th><th>환자 / 차트</th>${C.CHANNELS.map(k => `<th class="amount-cell">${k}</th>${k === '여신티켓' ? '<th>여신 사용</th>' : k === '강남언니' ? '<th>강언 사용</th>' : ''}`).join('')}<th>합계</th><th>메모</th><th>저장 상태</th><th></th></tr></thead><tbody>${sales.map((row, i) => `<tr><td>${i + 1}</td><td><div class="patient">${esc(row.raw.patient_name) || '입력 중'}<small>${esc(row.raw.chart_no) || '차트번호 미입력'}</small></div></td>${C.CHANNELS.map(k => `<td class="amount-cell">${cellAmount(row.raw, k)}</td>${k === '여신티켓' ? usage(row, 'yeoshin_used', '여신 사용') : k === '강남언니' ? usage(row, 'gangnamunni_used', '강언 사용') : ''}`).join('')}<td class="amount-cell">${rowAmount(row)}</td><td class="cell-note">${esc(row.raw.note) || '—'}</td><td class="status-cell">${badge(row)}${row.message ? `<small>${esc(row.message)}</small>` : ''}</td><td><button class="btn small" data-edit="${row.id}">${row.conflict || row.failed ? '검토' : '수정'}</button></td></tr>`).join('') || '<tr><td colspan="23"><div class="empty-state">수납 기록이 없습니다. 수납 등록으로 입력을 시작하세요.</div></td></tr>'}</tbody></table></div><div class="table-tail"><span>수납 ${sales.length}건 표시 · 가로로 스크롤하여 모든 항목 확인</span><strong>금액은 원 단위 정수</strong></div>`;
    $('#refund-table').innerHTML = `<div class="table-scroll"><table class="reconcile-table"><thead><tr><th>환자 / 차트</th><th>원수납</th><th>환불 사유</th><th>환불액</th><th>저장 상태</th><th></th></tr></thead><tbody>${refunds.map(r => `<tr><td><div class="patient">${esc(r.raw.patient_name) || '입력 중'}<small>${esc(r.raw.chart_no)}</small></div></td><td class="refund-id">${esc(r.raw.refund_of) || '원수납 미선택'}</td><td class="cell-note">${esc(r.raw.note) || '—'}</td><td class="amount-cell negative">−${rowAmount(r)}</td><td>${badge(r)}</td><td><button class="btn small" data-edit="${r.id}">수정</button></td></tr>`).join('') || '<tr><td colspan="6"><div class="empty-state">등록된 환불이 없습니다.</div></td></tr>'}</tbody></table></div>`;
    document.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openEditor(b.dataset.edit));
    document.querySelectorAll('[data-usage]').forEach(input => input.onchange = () => {
      const m = model(),
        row = m.rows.get(input.dataset.id);
      m.edit(row.id, input.dataset.usage, input.checked);
      controller.changed(scope);
    });
  }
  function bindMain() {
    if (tab === 'ledger') {
      renderTables();
      $('#new-receipt').onclick = () => openEditor();
      $('#new-refund').onclick = () => openEditor(null, 'refund');
      $('#ledger-search').oninput = e => {
        query = e.target.value;
        renderTables();
      };
    }
    $('#apply-range')?.addEventListener('click', () => {
      rangeFrom = $('#range-from').value;
      rangeTo = $('#range-to').value;
      loadHistory();
    });
    $('#download-backup')?.addEventListener('click', downloadBackup);
    $('#close-day')?.addEventListener('click', closeDay);
    $('#reopen-day')?.addEventListener('click', reopenDay);
    $('#refresh-events')?.addEventListener('click', loadActivity);
    bindEngineLinks();
  }
  function updateEditorStatus() {
    const m = models()?.get(editing?.scope),
      r = m?.rows.get(editing?.id);
    if (!r) return;
    const s = $('#editor-save-status');
    if (s) s.textContent = `${statusLabels[r.status] || '확인 필요'}${r.message ? ' · ' + r.message : ''}`;
    const action = $('#editor-state-actions');
    if (action) {
      action.innerHTML = r.failed ? '<button class="btn small" id="retry-save">동일 요청 재시도</button>' : r.conflict ? '<button class="btn small danger" id="resolve-current">서버값으로 되돌리기</button>' : '';
      $('#retry-save')?.addEventListener('click', () => controller.retry(editing.scope, r.id));
      $('#resolve-current')?.addEventListener('click', () => {
        const context = {
          ...editing
        };
        notice('최신 기록을 불러올까요?', '이 창의 입력을 버리고 서버의 최신 기록을 다시 조회합니다. 저장된 초안이 있다면 최신 초안이 표시됩니다.', async () => {
          const fresh = await T.request('/api/ledger?' + new URLSearchParams({
            branch: context.branch,
            date: context.date
          }));
          if (controller.stopped || !models().has(context.scope)) return;
          controller.replaceFromServer(context.scope, context.id, fresh.rows);
          if (context.scope === scope) {
            workspace = fresh;
            closeEditor();
            render();
          }
          toast('최신 기록을 불러왔습니다. 저장된 초안은 필요하면 초안 삭제로 정리해 주세요.');
        });
      });
    }
    $('#save-now')?.toggleAttribute('disabled', !!r.failed || !!r.conflict || !!r.voided || m.composing.has(r.id) || isClosed());
    $('#discard-draft')?.toggleAttribute('disabled', !!m.active || !!r.failed || !!r.conflict || isClosed());
    $('#void-receipt')?.toggleAttribute('disabled', !C.canVoid(r, m, isClosed()));
    if ($('#discard-draft')) $('#discard-draft').hidden = !!r.receipt_revision && !r.draft_revision;
    const fields = $('#receipt-fields');
    if (fields) fields.disabled = isClosed() || !!r.voided || !!r.conflict || !!r.failed;
    try {
      $('#form-total').textContent = C.won(C.total(r.raw)) + '원';
      $('#money-warning').textContent = '';
    } catch (error) {
      $('#form-total').textContent = '금액 확인 필요';
      $('#money-warning').textContent = error.message;
    }
    const same = [...m.rows.values()].filter(other => other.id !== r.id && !other.voided && String(r.raw.chart_no || '').trim() && String(other.raw.chart_no || '').trim() === String(r.raw.chart_no || '').trim());
    $('#duplicate-warning').textContent = same.length ? `같은 차트번호 기록 ${same.length}건 · 별도 수납인지 확인해 주세요. 동일 차트만으로 중복 거래를 확정하지 않습니다.` : '';
  }
  function openEditor(id = null, kind = 'receipt') {
    const m = model();
    if (!m) return;
    if (!id) {
      if (isClosed()) return;
      id = m.add(C.blank(kind));
    }
    const row = m.rows.get(id);
    editing = {
      scope,
      id,
      branch,
      date
    };
    const raw = row.raw,
      d = $('#editor-dialog');
    d.innerHTML = `<form id="receipt-form"><div class="dialog-head"><div><h2>${row.receipt_revision ? '원장 수정' : raw.kind === 'refund' ? '환불 등록' : '수납 등록'}</h2><p>${esc(branch)} · ${date} · 입력 후 1.2초 자동저장</p></div><button type="button" class="dialog-close" aria-label="닫기">×</button></div><div class="dialog-body"><fieldset id="receipt-fields" class="receipt-fields"><div class="form-grid"><label class="field">차트번호<input data-field="chart_no" name="chart_no" value="${esc(raw.chart_no)}" placeholder="차트번호" maxlength="100"></label><label class="field">환자명<input data-field="patient_name" name="patient_name" value="${esc(raw.patient_name)}" placeholder="환자명" maxlength="100"></label></div>${raw.kind === 'refund' ? `<div class="form-section">환불 원수납 선택</div><div class="refund-lookup"><label class="field">원수납 영업일<input id="original-date" type="date" value="${date}"></label><button type="button" class="btn" id="lookup-original">원수납 조회</button></div><label class="field">원수납<select id="original-select"><option value="">${raw.refund_of ? '현재 연결: ' + esc(raw.refund_of) : '영업일의 원수납을 조회해 선택하세요'}</option></select></label><div class="form-help" id="original-status">${raw.refund_of ? '원거래 ' + esc(raw.refund_of) : '원거래가 없으면 정식 환불로 저장되지 않습니다.'}</div>` : ''}<div class="form-section">${raw.kind === 'refund' ? '환불액' : '수납액'} · 모두 0 이상의 원 단위 정수</div><div class="money-grid">${C.CHANNELS.map((key, i) => `<label class="field">${key}<input name="money-${i}" data-payment="${key}" inputmode="numeric" type="text" value="${esc(raw.payments?.[key] ?? '')}" placeholder="0" maxlength="30"></label>`).join('')}</div><div class="money-summary"><span>${raw.kind === 'refund' ? '환불 합계 · 순수납에서 차감' : '수납 합계'}</span><b id="form-total"></b></div><div class="error-message" id="money-warning" role="status"></div><div class="form-section">사용 여부</div><div class="checkbox-row">${[['yeoshin_used', '여신사용'], ['gangnamunni_used', '강언사용']].map(([key, label]) => `<label><input type="checkbox" data-field="${key}" ${raw[key] ? 'checked' : ''}>${label}</label>`).join('')}</div><div class="form-grid memo-grid"><label class="field full">${raw.kind === 'refund' ? '환불 사유' : '메모'}<textarea data-field="note" maxlength="2000">${esc(raw.note)}</textarea></label></div><div class="form-help" id="duplicate-warning"></div></fieldset><div class="form-save-status" id="editor-save-status" role="status" aria-live="polite"></div><div class="form-actions" id="editor-state-actions"></div><div class="form-help">차트번호·환자명·금액이 모두 갖춰지면 수납으로 저장됩니다. 미완료 입력은 초안으로 보관되며 합계와 분석에서 제외됩니다.</div></div><div class="dialog-footer"><div class="form-actions">${row.receipt_revision ? '<button type="button" class="btn danger" id="void-receipt">수납 취소</button>' : ''}<button type="button" class="btn danger" id="discard-draft">${row.receipt_revision ? '초안 버리고 저장값 복원' : '초안 삭제'}</button></div><button type="button" class="btn" id="close-editor">닫기</button><button type="submit" class="btn primary" id="save-now">지금 저장</button></div></form>`;
    d.querySelector('.dialog-close').onclick = closeEditor;
    $('#close-editor').onclick = closeEditor;
    $('#receipt-form').onsubmit = e => {
      e.preventDefault();
      controller.flush(editing.scope);
    };
    d.querySelectorAll('[data-field],[data-payment]').forEach(input => {
      input.addEventListener('compositionstart', () => controller.composing(editing.scope, id, true));
      input.addEventListener('compositionend', () => {
        handleInput(input);
        controller.composing(editing.scope, id, false);
      });
      input.addEventListener('input', () => handleInput(input));
    });
    function handleInput(input) {
      if (!editing || editing.id !== id) return;
      const payment = input.dataset.payment,
        field = payment || input.dataset.field;
      m.edit(id, field, input.type === 'checkbox' ? input.checked : input.value, !!payment);
      controller.changed(editing.scope);
      updateEditorStatus();
    }
    $('#lookup-original')?.addEventListener('click', () => lookupOriginal(m, id));
    $('#discard-draft')?.addEventListener('click', () => notice('초안을 삭제할까요?', '입력 중인 초안을 삭제합니다. 이미 저장된 수납 내역은 삭제하지 않습니다.', () => {
      if (m.active || row.failed || row.conflict) return;
      m.discard(id);
      controller.flush(scope);
      closeEditor();
      renderTables();
    }));
    $('#void-receipt')?.addEventListener('click', () => voidReceipt(m, row));
    updateEditorStatus();
    d.showModal();
    d.querySelector('[data-field="chart_no"]')?.focus();
  }
  function closeEditor() {
    if (editing) {
      const m = models()?.get(editing.scope),
        r = m?.rows.get(editing.id);
      if (r?.status === 'new' && !r.dirty) m.rows.delete(r.id);
      m?.composing.delete(editing.id);
    }
    editing = null;
    $('#editor-dialog').close();
    renderTables();
  }
  async function lookupOriginal(m, id) {
    const context = editing,
      originalDate = $('#original-date').value;
    if (!originalDate) return;
    $('#original-status').textContent = '원수납 조회 중…';
    try {
      const data = await T.request('/api/ledger?' + new URLSearchParams({
        branch: context.branch,
        date: originalDate
      }));
      if (editing !== context) return;
      const originals = data.day.receipts.filter(r => r.kind === 'receipt' && !r.voided);
      $('#original-select').innerHTML = '<option value="">원수납을 선택하세요</option>' + originals.map(r => `<option value="${r.id}">${esc(r.patient_name)} · ${esc(r.chart_no)} · ${C.won(C.CHANNELS.reduce((n, k) => n + C.money(r.payments[k]), 0n))}원</option>`).join('');
      $('#original-status').textContent = originals.length ? `${originalDate} 원수납 ${originals.length}건` : '선택일에 저장된 원수납이 없습니다.';
      $('#original-select').onchange = e => {
        const original = originals.find(r => r.id === e.target.value);
        if (!original) return;
        for (const key of ['chart_no', 'patient_name']) {
          m.edit(id, key, original[key]);
          $(`[data-field="${key}"]`).value = original[key];
        }
        m.edit(id, 'refund_of', original.id);
        $('#original-status').textContent = '원거래 ' + original.id;
        controller.changed(context.scope);
      };
    } catch (error) {
      if (editing === context) $('#original-status').textContent = error.message;
    }
  }
  function notice(title, message, confirm = null, options = {}) {
    const d = $('#notice-dialog');
    d.innerHTML = `<div class="dialog-head"><div><h2>${esc(title)}</h2><p>${esc(branch)} · ${date}</p></div><button class="dialog-close" aria-label="닫기">×</button></div><div class="dialog-body"><p>${esc(message)}</p>${options.reason ? '<label class="field">사유<input id="notice-reason" maxlength="300" placeholder="처리 사유를 입력해 주세요"></label>' : ''}${options.review ? '<label class="checkbox-row"><input id="notice-reviewed" type="checkbox"> 수납원장 내용을 수동 검토했습니다.</label>' : ''}<div class="error-message" id="notice-error" role="alert"></div></div><div class="dialog-footer"><button class="btn cancel-notice">닫기</button>${confirm ? '<button class="btn primary confirm-notice">확인</button>' : ''}</div>`;
    d.querySelector('.dialog-close').onclick = () => d.close();
    d.querySelector('.cancel-notice').onclick = () => d.close();
    d.querySelector('.confirm-notice')?.addEventListener('click', async e => {
      const reason = $('#notice-reason')?.value.trim();
      if (options.reason && !reason) {
        $('#notice-error').textContent = '처리 사유를 입력해 주세요.';
        return;
      }
      if (options.review && !$('#notice-reviewed').checked) {
        $('#notice-error').textContent = '검토 확인에 체크해 주세요.';
        return;
      }
      e.target.disabled = true;
      try {
        await confirm(reason);
        d.close();
      } catch (error) {
        $('#notice-error').textContent = error.message;
        e.target.disabled = false;
      }
    });
    d.showModal();
  }
  function voidReceipt(m, row) {
    const selectedScope = scope;
    notice('수납 기록을 취소할까요?', '삭제 대신 취소 이력을 남기며, 합계에서 제외합니다.', reason => {
      if (!C.canVoid(row, m, isClosed())) throw Error('먼저 현재 입력을 수납으로 저장하거나 미완료 초안을 버려 주세요.');
      row.raw._void_reason = reason;
      row.action = 'void';
      row.dirty = true;
      controller.flush(selectedScope);
      closeEditor();
    }, {
      reason: true
    });
  }
  function closingView() {
    const receipts = workspace.day.receipts.filter(r => !r.voided),
      sum = (key, kind) => receipts.filter(r => r.kind === kind).reduce((n, r) => n + C.money(r.payments[key]), 0n);
    return heading('DAILY CLOSE', '하루의 끝, 빠짐없는 마감', '저장된 결제수단별 수납과 환불을 검토하고 원장 스냅샷을 보관합니다.', isClosed() ? '<button class="btn" id="reopen-day">사유를 남기고 재개방</button>' : '<button class="btn primary" id="close-day">마감 검토하기 →</button>') + statCards() + `<div class="closing-grid"><section class="panel"><div class="panel-head"><div><div class="panel-title">결제수단별 마감</div><div class="panel-subtitle">저장된 원장 기준 · 환불은 양의 차감액으로 표시</div></div></div><table class="closing-table"><thead><tr><th>결제수단</th><th>수납</th><th>환불</th><th>순수납</th></tr></thead><tbody>${C.CHANNELS.map(key => `<tr><td>${key}</td><td>${C.won(sum(key, 'receipt'))}</td><td>${C.won(sum(key, 'refund'))}</td><td>${C.won(sum(key, 'receipt') - sum(key, 'refund'))}</td></tr>`).join('')}</tbody></table></section><section class="panel"><div class="panel-head"><div class="panel-title">마감 전 확인</div><span class="badge ${isClosed() ? 'ok' : 'review'}">${isClosed() ? '마감됨' : '검토 필요'}</span></div><div class="neutral-notice"><h3>수납원장 개정 ${workspace.day.revision}</h3><p>전체 미완료 초안 ${workspace.pending_count}건 (다른 사용자의 초안 포함) · 이 브라우저 ${allPending() ? '저장 대기/확인할 입력 있음' : '전송 대기 없음'}</p><p>마감은 현재 원장을 고정하는 기능입니다. 단말기 마감·베가스 마감과의 대조 완료를 뜻하지 않습니다.</p><button class="btn analysis-link" data-engine="workbench">마감 파일 업로드·대조하기 →</button></div></section></div>`;
  }
  async function closeDay() {
    if (allPending() || workspace.pending_count) {
      notice('저장 상태를 먼저 확인해 주세요', '미전송 입력과 미완료 초안을 모두 처리한 뒤 마감할 수 있습니다. 다른 사용자의 초안은 해당 사용자에게 확인하거나 A 버전의 총괄 초안 관리에서 정리해 주세요.');
      return;
    }
    const context = {
      branch,
      date,
      scope,
      request_id: crypto.randomUUID(),
      expected_revision: workspace.day.revision
    };
    notice('현재 원장을 마감할까요?', '현재 개정의 수납원장 스냅샷이 저장되고 입력이 잠깁니다. 다른 사용자가 수정했다면 최신 원장을 다시 검토해야 합니다.', async () => {
      if (allPending()) throw Error('저장 대기 중인 입력이 있습니다.');
      await T.request('/api/ledger/close', {
        method: 'POST',
        body: context
      });
      await refreshWorkspace(false);
      render();
      toast('원장 마감 스냅샷을 저장했습니다.');
    }, {
      review: true
    });
  }
  function reopenDay() {
    const context = {
      branch,
      date,
      scope,
      request_id: crypto.randomUUID(),
      expected_revision: workspace.day.revision
    };
    notice('원장을 재개방할까요?', '기존 마감 스냅샷을 보존하고 수정할 수 있도록 다시 엽니다.', async reason => {
      await T.request('/api/ledger/reopen', {
        method: 'POST',
        body: {
          ...context,
          reason
        }
      });
      await refreshWorkspace(false);
      render();
      toast('원장을 재개방했습니다.');
    }, {
      reason: true
    });
  }
  function historyView() {
    return heading('DAILY HISTORY', '기간별 기록', '이전 영업일의 기록과 마감 상태를 확인하고 다시 열어 보세요.', '<button class="btn" data-engine="reconciliation-history">정산 결과 기록</button><button class="btn" id="download-backup">↓ 검증 백업 다운로드</button>') + `<section class="panel"><div class="history-filter"><label class="field">시작일<input id="range-from" type="date" value="${rangeFrom}"></label><span>—</span><label class="field">종료일<input id="range-to" type="date" value="${rangeTo}"></label><button class="btn primary" id="apply-range">조회</button><small>한국 영업일 기준</small></div><div id="history-results" class="loading-line">기록을 불러오는 중입니다.</div></section><div class="info-banner stack-gap">ⓘ <span>다운로드는 전체 원장 검증 백업입니다. 자동 백업 예약이나 별도 보관을 대신하지 않습니다.</span></div>`;
  }
  async function loadHistory() {
    const sequence = ++extraSequence,
      b = branch;
    if (!rangeFrom || !rangeTo || rangeFrom > rangeTo) {
      $('#history-results').textContent = '조회 기간을 확인해 주세요.';
      return;
    }
    try {
      const data = await T.request('/api/ledger/days?' + new URLSearchParams({
        branch: b,
        start: rangeFrom,
        end: rangeTo
      }));
      if (tab !== 'history' || sequence !== extraSequence || b !== branch) return;
      $('#history-results').className = '';
      $('#history-results').innerHTML = `<div class="table-scroll"><table class="reconcile-table history-table"><thead><tr><th>영업일</th><th>상태</th><th>원장 개정</th><th>최근 변경</th><th></th></tr></thead><tbody>${data.days.map(d => `<tr><td><strong>${esc(d.business_date)}</strong></td><td><span class="badge ${d.state === 'closed' ? 'ok' : 'review'}">${d.state === 'closed' ? '마감' : '입력 중'}</span></td><td>${esc(d.revision)}</td><td>${esc(d.updated_at || '—')}</td><td><button class="btn small" data-open-date="${esc(d.business_date)}">원장 열기</button></td></tr>`).join('') || '<tr><td colspan="5"><div class="empty-state">해당 기간의 저장된 기록이 없습니다.</div></td></tr>'}</tbody></table></div>`;
      document.querySelectorAll('[data-open-date]').forEach(b => b.onclick = () => switchDate(b.dataset.openDate, 'ledger'));
    } catch (error) {
      if (tab === 'history' && sequence === extraSequence) $('#history-results').textContent = error.message;
    }
  }
  function activityView() {
    return heading('CHANGE HISTORY', '변경 이력', '선택한 영업일의 저장 이력과 마감 스냅샷을 확인합니다.', '<button class="btn" id="refresh-events">↻ 이력 새로고침</button>') + `<section class="panel"><div class="panel-head"><div class="panel-title">마감 스냅샷</div></div><div id="snapshots" class="snapshot-list">조회 중…</div></section><section class="panel stack-gap"><div class="panel-head"><div class="panel-title">최근 작업 기록</div></div><div id="events" class="loading-line">조회 중…</div></section>`;
  }
  async function loadActivity() {
    const sequence = ++extraSequence,
      p = params();
    const results = await Promise.allSettled([T.request('/api/ledger/events?' + p), T.request('/api/ledger/snapshots?' + p)]);
    if (tab !== 'activity' || sequence !== extraSequence) return;
    const [e, s] = results;
    if (e.status === 'rejected') $('#events').textContent = e.reason.message;else {
      $('#events').className = '';
      $('#events').innerHTML = `<ul class="activity-list">${e.value.events.map(item => `<li><i class="activity-dot"></i><span><strong>${esc(item.action || item.event_type || item.operation || '변경')}</strong> · ${esc(item.actor || item.username || item.updated_by || '')}<small class="history-state"> ${esc(item.receipt_id || item.entity_id || item.after?.id || item.before?.id || '')}</small></span><time>${esc(item.occurred_at || item.created_at || item.timestamp || '')}</time></li>`).join('') || '<li>이 영업일의 변경 기록이 없습니다.</li>'}</ul>`;
    }
    if (s.status === 'rejected') $('#snapshots').textContent = s.reason.message;else {
      $('#snapshots').innerHTML = s.value.snapshots.map((item, i) => `<button class="btn small" data-snapshot="${i}">마감 ${esc(item.close_sequence || i + 1)} · ${esc(item.closed_at || item.created_at || '')} ↓</button>`).join('') || '저장된 마감 스냅샷이 없습니다.';
      document.querySelectorAll('[data-snapshot]').forEach(button => button.onclick = () => {
        const item = s.value.snapshots[Number(button.dataset.snapshot)];
        if (typeof item.download_json !== 'string') {
          toast('스냅샷 원본 다운로드를 확인하지 못했습니다. 다시 조회해 주세요.');
          return;
        }
        download(new Blob([item.download_json], {
          type: 'application/json'
        }), `ledger-snapshot-${date}-${Number(button.dataset.snapshot) + 1}.json`);
      });
    }
  }
  function engineView() {
    return heading('RECONCILIATION WORKSPACE', '마감 파일 대조', '단말기 마감·일일마감·베가스 마감을 비교하고 차이의 근거를 확인합니다.') + statCards() + `<section class="panel"><div class="panel-head"><div><div class="panel-title">분석 자료 준비</div><div class="panel-subtitle">${esc(branch)} · ${date}</div></div></div><div class="neutral-notice"><h3>하루 정밀 분석과 기간 분석</h3><p>현재 원장, 구글시트 또는 업로드 파일을 베가스 수납 파일과 대조합니다. 결과에서 환자별 검증·AI 설명·엑셀 다운로드를 사용할 수 있습니다.</p><button class="btn primary" data-engine="workbench">하루 마감 파일 올리기</button> <button class="btn" data-engine="period">기간 자료 대조</button> <button class="btn" data-engine="reconciliation-history">정산 결과 기록</button>${T.mode === 'demo' ? '<p>가상 자료 체험 화면입니다. 실제 파일 분석은 정산앱에서 사용할 수 있습니다.</p>' : ''}</div></section>`;
  }
  function bindEngineLinks() {
    document.querySelectorAll('[data-engine]').forEach(a => a.onclick = () => navigateEngine(a.dataset.engine || 'workbench'));
  }
  async function navigateEngine(destination) {
    if (allPending()) {
      toast('저장 대기 또는 확인할 입력이 있습니다. 저장 상태를 먼저 확인해 주세요.');
      return;
    }
    try {
      if (!T.navigate) {
        toast('실제 파일 대조는 로그인한 정산앱에서 사용할 수 있습니다.');
        return;
      }
      await T.navigate(destination, {branch, date});
    } catch (error) {
      toast(error.message);
    }
  }
  async function downloadBackup() {
    const button = $('#download-backup');
    button.disabled = true;
    try {
      const blob = await T.request('/api/ledger/backup', {
        blob: true
      });
      download(blob, `clinic-pay-ledger-backup-${date}.json`);
      toast(T.mode === 'demo' ? '가상 예시 기록을 내려받았습니다.' : '검증 백업 다운로드를 요청했습니다.');
    } catch (error) {
      toast(error.message);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob),
      a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value < '0001-01-01' || value > '9999-12-31') return false;
    const d = new Date(value + 'T12:00:00Z');
    return !Number.isNaN(d.valueOf()) && d.toISOString().slice(0, 10) === value;
  }
  async function switchDate(value, nextTab = tab) {
    if (!validDate(value)) {
      toast('올바른 영업일을 선택해 주세요.');
      $('#business-date').value = date;
      return;
    }
    closeEditor();
    $('#notice-dialog').close();
    date = value;
    tab = nextTab;
    query = '';
    extraSequence++;
    await refreshWorkspace(true);
  }
  $('#login-form').onsubmit = async e => {
    e.preventDefault();
    const button = $('#login-submit');
    button.disabled = true;
    $('#login-error').textContent = '';
    try {
      session = await T.request('/api/session', {
        method: 'POST',
        body: {
          username: $('#username').value,
          password: $('#password').value
        }
      });
      $('#password').value = '';
      await enterApp();
    } catch (error) {
      $('#login-error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  $('#logout').onclick = () => {
    if (allPending()) {
      notice('저장 상태를 먼저 확인해 주세요', '저장 대기·실패·충돌 입력이 있습니다. 입력을 보존하려면 모두 처리한 후 로그아웃하세요.');
      return;
    }
    notice('로그아웃할까요?', '이 브라우저의 원장 화면과 로그인 정보를 지웁니다.', async () => {
      session = await T.request('/api/session', {
        method: 'DELETE'
      });
      controller.stop();
      workspace = null;
      scope = '';
      $('#main-content').replaceChildren();
      showLogin();
    });
  };
  $('#mobile-logout').onclick = () => $('#logout').click();
  $('#branch-select').onchange = async e => {
    closeEditor();
    $('#notice-dialog').close();
    branch = e.target.value;
    query = '';
    extraSequence++;
    await refreshWorkspace(true);
  };
  $('#business-date').onchange = e => switchDate(e.target.value);
  document.querySelectorAll('[data-date-step]').forEach(button => button.onclick = () => {
    const d = new Date(date + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + Number(button.dataset.dateStep));
    switchDate(d.toISOString().slice(0, 10));
  });
  $('#refresh-day').onclick = () => refreshWorkspace(false);
  document.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => {
    if (T.navigate && ['workbench', 'analysis', 'reconciliation-history'].includes(button.dataset.tab)) {
      navigateEngine(button.dataset.tab);
      return;
    }
    closeEditor();
    tab = button.dataset.tab;
    extraSequence++;
    history.replaceState(null, '', '#' + tab);
    render();
    window.scrollTo({
      top: 0
    });
  });
  for (const link of [$('.brand'), $('#settlement-home')]) link.onclick = e => {
    e.preventDefault();
    tab = 'ledger';
    render();
  };
  $('.return-a').onclick = e => {
    if (T.navigate) {
      e.preventDefault();
      navigateEngine('table');
    }
  };
  $('#editor-dialog').addEventListener('cancel', e => {
    e.preventDefault();
    closeEditor();
  });
  window.addEventListener('beforeunload', e => {
    if (allPending()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && workspace) refreshWorkspace(false);
  });
  if (window.LedgerEmbedded) document.documentElement.classList.add('ledger-embedded');
  if (T.mode === 'demo') {
    $('#environment-label').textContent = 'B 버전 체험 · 가상 데이터 · 실제 시스템 미연결';
    $('#environment-detail').textContent = '새로고침하면 초기화됩니다';
    $('#footer-note').textContent = '가상 데이터 · 이 화면의 변경은 메모리에만 유지됩니다.';
  }
  bootstrap();
})();
