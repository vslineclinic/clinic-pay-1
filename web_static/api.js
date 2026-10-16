(function (root) {
  'use strict';

  if (root.LedgerTransport) return;
  let csrf = '';
  root.LedgerTransport = {
    mode: 'live',
    async request(path, {
      method = 'GET',
      body,
      blob = false
    } = {}) {
      const headers = {
        Accept: 'application/json'
      };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
      const response = await fetch(path, {
        method,
        headers,
        credentials: 'same-origin',
        cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      if (blob && response.ok) return response.blob();
      let data;
      try {
        data = await response.json();
      } catch {
        throw Object.assign(Error('서버 응답을 확인하지 못했습니다. 잠시 후 다시 시도하세요.'), {
          status: response.status
        });
      }
      if (!response.ok) throw Object.assign(Error(data.error?.message || '요청을 처리하지 못했습니다.'), {
        status: response.status,
        code: data.error?.code,
        current: data.error?.current
      });
      if (typeof data.csrf_token === 'string') csrf = data.csrf_token;
      return data;
    }
  };
})(window);
