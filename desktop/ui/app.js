// desktop/ui/app.js — frontend state + bridge to pywebview.api
// 全部 DOM 构造走 createElement + textContent, 避免 innerHTML 的 XSS 面.

const $ = (sel) => document.querySelector(sel);

const state = {
  bootstrapped: false,
  endpoints: [],
  groups: [],
  defaultKey: null,
  defaultEndpoint: null,
  pingResults: {}, // url → {ok, latency_ms, summary} | {running:true}
  groupResults: {}, // group_id → {chat:{...}, image:{...}}
};

// ---- screen routing ----

function showScreen(id) {
  for (const s of ['loading', 'error', 'dashboard']) {
    $(`#${s}`).classList.toggle('hidden', s !== id);
  }
}

function setStatus(text, cls = '') {
  const el = $('#status');
  el.textContent = text;
  el.className = 'status' + (cls ? ' ' + cls : '');
}

function setBrandSubtitle(text) {
  $('#brand-subtitle').textContent = text;
}

function showError(title, msg) {
  $('#error-title').textContent = title;
  $('#error-msg').textContent = msg || '(空)';
  showScreen('error');
}

// ---- formatting ----

function fmtMoney(v) {
  if (typeof v !== 'number') return '?';
  return '$' + v.toFixed(4);
}

function fmtLatency(ms) {
  if (typeof ms !== 'number') return '?';
  return ms + 'ms';
}

function fmtRate(r) {
  if (r === null || r === undefined) return '?';
  return r + 'x';
}

// build a <span class="tag ..."> element for a probe/test result cell
function buildTag(result) {
  const span = document.createElement('span');
  span.className = 'tag';
  if (!result) {
    span.classList.add('dim');
    span.textContent = '—';
    return span;
  }
  if (result.running) {
    span.classList.add('running');
    span.textContent = '⏳';
    return span;
  }
  if (result.ok) {
    span.classList.add('ok');
    span.textContent = `✓ ${fmtLatency(result.latency_ms)}`;
    return span;
  }
  span.classList.add('err');
  span.textContent = `✗ ${result.status ?? '?'}`;
  return span;
}

function el(tag, opts = {}) {
  const e = document.createElement(tag);
  if (opts.className) e.className = opts.className;
  if (opts.text != null) e.textContent = String(opts.text);
  if (opts.children) for (const c of opts.children) if (c) e.appendChild(c);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  if (opts.onClick) e.addEventListener('click', opts.onClick);
  return e;
}

// ---- bootstrap ----

async function bootstrap() {
  showScreen('loading');
  setStatus('连接中…');
  try {
    const data = await window.pywebview.api.bootstrap();
    if (!data.ok) {
      const title = data.needs_login ? '需登录' : (data.needs_setup ? '尚未配置' : '错误');
      showError(title, data.error || '未知错误');
      setStatus('未就绪', 'err');
      return;
    }
    applyBootstrap(data);
    setStatus('✓ 已连接', 'ok');
    showScreen('dashboard');
  } catch (err) {
    showError('调用 bootstrap 失败', err && err.message ? err.message : String(err));
    setStatus('错误', 'err');
  }
}

async function refresh() {
  setStatus('刷新中…', 'warn');
  try {
    const data = await window.pywebview.api.refresh();
    if (!data.ok) {
      setStatus('刷新失败', 'err');
      showError(data.needs_login ? '需登录' : '错误', data.error);
      return;
    }
    state.pingResults = {};
    state.groupResults = {};
    applyBootstrap(data);
    setStatus('✓ 已刷新', 'ok');
  } catch (err) {
    showError('刷新失败', err && err.message ? err.message : String(err));
    setStatus('错误', 'err');
  }
}

function applyBootstrap(data) {
  state.bootstrapped = true;
  state.endpoints = data.endpoints || [];
  state.groups = data.groups || [];
  state.defaultKey = data.default_key;
  state.defaultEndpoint = data.default_endpoint;
  setBrandSubtitle(data.site || data.domain || '');
  renderAccount(data);
  renderDefaultKey(data);
  renderEndpoints(data.endpoints || [], data.default_endpoint);
  renderGroups(data.groups || [], data.keys || [], data.default_key);
}

// ---- renderers ----

function renderAccount(data) {
  const u = data.user || {};
  $('#acc-email').textContent = u.email || '—';
  const status = u.status || '—';
  const statusEl = $('#acc-status');
  statusEl.textContent = status;
  statusEl.className = 'v';
  if (status === 'active') statusEl.classList.add('num');
  $('#acc-balance').textContent = fmtMoney(u.balance);
  $('#acc-concurrency').textContent = u.concurrency != null ? String(u.concurrency) : '—';
}

function renderDefaultKey(data) {
  const k = data.default_key;
  if (!k) {
    $('#key-name').textContent = '—';
    $('#key-group').textContent = '—';
    $('#key-value').textContent = '—';
    $('#ep-current').textContent = '—';
    $('#btn-reveal').disabled = true;
    return;
  }
  $('#key-name').textContent = k.name || '—';
  $('#key-group').textContent = `${k.group_name || '?'} (${fmtRate(k.group_rate)})`;
  $('#key-value').textContent = k.key_masked || '—';
  $('#btn-reveal').disabled = false;
  $('#btn-reveal').textContent = '显示';
  const ep = data.default_endpoint;
  $('#ep-current').textContent = ep ? ep.endpoint : '—';
}

function buildEndpointRow(ep, isCurrent, pingResult) {
  const tr = document.createElement('tr');
  if (isCurrent) tr.classList.add('current');

  tr.appendChild(el('td', {
    children: [isCurrent ? el('span', { className: 'star', text: '★' }) : null],
  }));
  tr.appendChild(el('td', { text: ep.name || '?' }));
  tr.appendChild(el('td', { className: 'mono', text: ep.endpoint || '' }));

  const latencyTd = el('td', { className: 'right' });
  latencyTd.appendChild(buildTag(pingResult));
  tr.appendChild(latencyTd);

  const actionsTd = el('td', { className: 'right' });
  actionsTd.appendChild(el('button', {
    className: 'link sm',
    text: 'ping',
    onClick: () => pingOne(ep.endpoint),
  }));
  if (!isCurrent) {
    actionsTd.appendChild(el('button', {
      className: 'link sm',
      text: '设默认',
      onClick: () => setDefaultEndpoint(ep.name),
    }));
  }
  tr.appendChild(actionsTd);

  return tr;
}

function renderEndpoints(endpoints, defaultEp) {
  const tbody = $('#t-endpoints-body');
  const frag = document.createDocumentFragment();
  for (const ep of endpoints) {
    const isCur = defaultEp && defaultEp.name === ep.name;
    const r = state.pingResults[ep.endpoint];
    frag.appendChild(buildEndpointRow(ep, isCur, r));
  }
  tbody.replaceChildren(frag);
}

function buildGroupRow(g, isDefaultHere, groupResult) {
  const tr = document.createElement('tr');
  if (isDefaultHere) tr.classList.add('current');

  tr.appendChild(el('td', {
    children: [isDefaultHere ? el('span', { className: 'star', text: '★' }) : null],
  }));
  tr.appendChild(el('td', { text: fmtRate(g.rate_multiplier) }));
  tr.appendChild(el('td', { text: g.name || '?' }));

  const chatTd = el('td', { className: 'right' });
  chatTd.appendChild(buildTag(groupResult && groupResult.chat));
  tr.appendChild(chatTd);

  const imgTd = el('td', { className: 'right' });
  imgTd.appendChild(buildTag(groupResult && groupResult.image));
  tr.appendChild(imgTd);

  const actionsTd = el('td', { className: 'right' });
  actionsTd.appendChild(el('button', {
    className: 'link sm',
    text: '测试',
    onClick: () => testGroup(g.id),
  }));
  tr.appendChild(actionsTd);

  return tr;
}

function renderGroups(groups, _keys, defaultKey) {
  const tbody = $('#t-groups-body');
  const sorted = [...groups].sort((a, b) => (a.rate_multiplier ?? 99999) - (b.rate_multiplier ?? 99999));
  const frag = document.createDocumentFragment();
  for (const g of sorted) {
    const isDefaultHere = defaultKey && defaultKey.group_id === g.id;
    const r = state.groupResults[g.id];
    frag.appendChild(buildGroupRow(g, isDefaultHere, r));
  }
  tbody.replaceChildren(frag);
}

// ---- async actions ----

async function pingOne(url) {
  state.pingResults[url] = { running: true };
  renderEndpoints(state.endpoints, state.defaultEndpoint);
  try {
    state.pingResults[url] = await window.pywebview.api.ping_endpoint(url, false);
  } catch (err) {
    state.pingResults[url] = { ok: false, status: 'err', summary: String(err) };
  }
  renderEndpoints(state.endpoints, state.defaultEndpoint);
}

async function pingAll() {
  for (const ep of state.endpoints) state.pingResults[ep.endpoint] = { running: true };
  renderEndpoints(state.endpoints, state.defaultEndpoint);
  await Promise.all(state.endpoints.map(async (ep) => {
    try {
      state.pingResults[ep.endpoint] = await window.pywebview.api.ping_endpoint(ep.endpoint, false);
    } catch (err) {
      state.pingResults[ep.endpoint] = { ok: false, status: 'err', summary: String(err) };
    }
    renderEndpoints(state.endpoints, state.defaultEndpoint);
  }));
}

async function setDefaultEndpoint(name) {
  setStatus('切端点中…', 'warn');
  try {
    const r = await window.pywebview.api.set_default_endpoint(name);
    if (!r.ok) {
      setStatus('切端点失败', 'err');
      return;
    }
    state.defaultEndpoint = r.default_endpoint;
    $('#ep-current').textContent = r.default_endpoint.endpoint;
    renderEndpoints(state.endpoints, state.defaultEndpoint);
    setStatus('✓ 已切端点', 'ok');
  } catch (err) {
    setStatus('错误', 'err');
  }
}

async function testGroup(groupId) {
  state.groupResults[groupId] = { chat: { running: true }, image: { running: true } };
  renderGroups(state.groups, [], state.defaultKey);
  setStatus(`测试分组 ${groupId}…`, 'warn');
  try {
    const r = await window.pywebview.api.test_group(groupId);
    if (!r.ok) {
      state.groupResults[groupId] = {
        chat: { ok: false, status: 'err', summary: r.error },
        image: { ok: false, status: 'err', summary: r.error },
      };
      setStatus(r.error || '测试失败', 'err');
    } else {
      state.groupResults[groupId] = { chat: r.chat, image: r.image };
      state.defaultKey = r.default_key;
      setStatus('✓ 分组测试完成', 'ok');
    }
  } catch (err) {
    state.groupResults[groupId] = {
      chat: { ok: false, status: 'err', summary: String(err) },
      image: { ok: false, status: 'err', summary: String(err) },
    };
    setStatus('错误', 'err');
  }
  renderGroups(state.groups, [], state.defaultKey);
}

async function revealKey() {
  try {
    const r = await window.pywebview.api.reveal_default_key();
    if (!r.ok) {
      setStatus(r.error, 'err');
      return;
    }
    $('#key-value').textContent = r.key;
    $('#btn-reveal').textContent = '已显示';
    $('#btn-reveal').disabled = true;
  } catch (err) {
    setStatus('错误', 'err');
  }
}

// ---- wiring ----

window.addEventListener('pywebviewready', () => {
  setStatus('桥就绪, bootstrap…', 'warn');
  bootstrap();
});

$('#btn-refresh').addEventListener('click', () => {
  if (!state.bootstrapped) bootstrap();
  else refresh();
});

$('#btn-retry').addEventListener('click', () => {
  bootstrap();
});

$('#btn-reveal').addEventListener('click', revealKey);

$('#btn-ping-all').addEventListener('click', pingAll);
