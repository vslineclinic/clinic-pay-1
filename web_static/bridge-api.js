/* Streamlit transport only. Plain standalone pages continue to use api.js. */
(function () {
  'use strict';
  const streamlitUrl = new URLSearchParams(window.location.search).get('streamlitUrl');
  if (!streamlitUrl || window.parent === window) return;
  let origin;
  try { origin = new URL(streamlitUrl).origin; } catch (_) { return; }
  window.LedgerEmbedded = true;
  let session = null, active = null, ready = false, firstSession = true;
  const queue = [], pendingReads = new Map();
  let readyResolve;
  const readiness = new Promise(resolve => { readyResolve = resolve; });
  const send = (type, extra) => window.parent.postMessage({isStreamlitMessage: true, type, ...extra}, origin);
  const failure = (message, status = 503, code = 'unavailable', current) => {
    const error = new Error(message); Object.assign(error, {status, code, current}); return error;
  };
  function drain() {
    if (!ready || active || !queue.length) return;
    active = queue.shift();
    const request = active;
    request.timer = setTimeout(() => {
      if (active !== request) return;
      active = null;
      request.reject(failure('서버 응답이 늦어지고 있습니다. 입력을 유지하고 다시 시도해 주세요.'));
      drain();
    }, 45000);
    send('streamlit:setComponentValue', {dataType: 'json', value: {
      request_id: request.id, path: request.path, method: request.method, body: request.body,
    }});
  }
  function receive(event) {
    if (event.source !== window.parent || event.origin !== origin || event.data?.type !== 'streamlit:render') return;
    const args = event.data.args || {};
    session = args.session || session;
    if (!ready) { ready = true; readyResolve(); }
    const response = args.response;
    if (response && active && response.request_id === active.id) {
      const request = active; active = null; clearTimeout(request.timer);
      if (response.status >= 400) {
        const error = response.data?.error || {};
        request.reject(failure(error.message || '요청을 완료하지 못했습니다.', response.status, error.code, error.current));
      } else {
        const result = request.blob ? new Blob([JSON.stringify(response.data)], {type: 'application/json'}) : response.data;
        request.resolve(result);
      }
    }
    drain();
  }
  window.addEventListener('message', receive);
  async function request(path, options = {}) {
    await readiness;
    const method = options.method || 'GET';
    if (path === '/api/session' && method === 'GET' && firstSession && session) {
      firstSession = false; return session;
    }
    const readKey = method === 'GET' ? `${path}:${Boolean(options.blob)}` : null;
    if (readKey && pendingReads.has(readKey)) return pendingReads.get(readKey);
    const promise = new Promise((resolve, reject) => {
      queue.push({id: crypto.randomUUID(), path, method, body: options.body || {}, blob: options.blob,
                  resolve, reject});
      drain();
    });
    if (readKey) pendingReads.set(readKey, promise);
    try { return await promise; }
    finally { if (readKey && pendingReads.get(readKey) === promise) pendingReads.delete(readKey); }
  }
  window.LedgerTransport = {
    request,
    navigate: (target, workspace = {}) => request('/api/navigation', {method: 'POST', body: {target, ...workspace}}),
  };
  send('streamlit:componentReady', {apiVersion: 1});
  send('streamlit:setFrameHeight', {height: 1000});
})();
