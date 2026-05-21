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
  refreshSidebar();  // sidebar is independent of bootstrap data
}

// ---- sidebar (multi-relay) ----

async function refreshSidebar() {
  const list = $('#sidebar-list');
  try {
    const r = await window.pywebview.api.list_relays_full();
    if (!r.ok) {
      list.replaceChildren(el('div', {
        className: 'muted small',
        text: r.error || '(空)',
      }));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const relay of r.relays) {
      const item = el('div', { className: 'sidebar-item' + (relay.is_current ? ' current' : '') });
      item.appendChild(el('div', { className: 'sidebar-site', text: relay.site || relay.domain }));
      const metaText = [
        relay.default_key_name ? `key=${relay.default_key_name}` : null,
        relay.default_endpoint_name ? `线路=${relay.default_endpoint_name}` : null,
      ].filter(Boolean).join(' · ');
      if (metaText) {
        item.appendChild(el('div', { className: 'sidebar-meta', text: metaText }));
      }
      if (!relay.is_current) {
        item.addEventListener('click', () => switchRelay(relay.domain));
      }
      frag.appendChild(item);
    }
    if (!r.relays.length) {
      frag.appendChild(el('div', { className: 'muted small', text: '(尚无 relay; 跑 ./sub2cli 走 wizard)' }));
    }
    list.replaceChildren(frag);
  } catch (err) {
    list.replaceChildren(el('div', { className: 'muted small', text: '错: ' + String(err) }));
  }
}

async function switchRelay(domain) {
  setStatus(`切到 ${domain}…`, 'warn');
  showScreen('loading');
  try {
    const data = await window.pywebview.api.switch_relay(domain);
    if (!data.ok) {
      showError(data.needs_login ? '需登录' : '切 relay 失败', data.error);
      setStatus('未就绪', 'err');
      return;
    }
    state.pingResults = {};
    state.groupResults = {};
    applyBootstrap(data);
    showScreen('dashboard');
    setStatus('✓ 已切 relay', 'ok');
  } catch (err) {
    showError('切 relay 失败', err && err.message ? err.message : String(err));
    setStatus('错误', 'err');
  }
}

// ---- inject modal (dry-run gate + apply) ----

function openInjectModal() {
  $('#inject-modal').classList.remove('hidden');
  $('#btn-inject-confirm').disabled = true;
  loadInjectPlan();
}

function closeInjectModal() {
  $('#inject-modal').classList.add('hidden');
}

async function loadInjectPlan() {
  const body = $('#inject-body');
  body.replaceChildren(el('div', {
    className: 'loader small',
    children: [el('div', { className: 'spinner' })],
  }));
  try {
    const r = await window.pywebview.api.inject_plan();
    if (!r.ok) {
      body.replaceChildren(
        el('div', { className: 'plan-label plan-result err', text: '错误' }),
        el('pre', { className: 'plan-preview', text: r.error + (r.stderr ? '\n\nstderr:\n' + r.stderr : '') }),
      );
      $('#btn-inject-confirm').disabled = true;
      return;
    }
    body.replaceChildren(
      el('div', { className: 'plan-label', text: r.label || '' }),
      el('pre', { className: 'plan-preview', text: r.plan_text || '(空)' }),
    );
    $('#btn-inject-confirm').disabled = false;
  } catch (err) {
    body.replaceChildren(
      el('div', { className: 'plan-label plan-result err', text: '调用 inject_plan 失败' }),
      el('pre', { className: 'plan-preview', text: err && err.message ? err.message : String(err) }),
    );
    $('#btn-inject-confirm').disabled = true;
  }
}

async function applyInject() {
  const confirmBtn = $('#btn-inject-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '执行中…';
  setStatus('注入中…', 'warn');
  try {
    const r = await window.pywebview.api.inject_apply();
    const body = $('#inject-body');
    body.replaceChildren(
      el('div', {
        className: 'plan-label ' + (r.ok ? 'plan-result ok' : 'plan-result err'),
        text: r.ok ? '✓ 注入完成 (rc=0)' : `✗ 注入失败 (rc=${r.returncode ?? '?'})`,
      }),
      el('pre', {
        className: 'plan-preview',
        text: (r.stdout || '') + (r.stderr ? '\n\nstderr:\n' + r.stderr : ''),
      }),
    );
    confirmBtn.textContent = r.ok ? '✓ 完成' : '✗ 失败';
    setStatus(r.ok ? '✓ 注入完成' : '✗ 注入失败', r.ok ? 'ok' : 'err');
  } catch (err) {
    setStatus('错误', 'err');
    confirmBtn.textContent = '✗ 失败';
  }
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

// ---- health modal ----

function openHealthModal() {
  $('#health-modal').classList.remove('hidden');
  runHealthCheck();
}

function closeHealthModal() {
  $('#health-modal').classList.add('hidden');
}

function buildCheckRow(check) {
  const row = el('div', { className: 'check-row' });

  const iconClass = check.severity || (check.ok ? 'ok' : 'err');
  const iconText = (
    iconClass === 'ok' ? '✓' :
    iconClass === 'warn' ? '!' :
    iconClass === 'running' ? '⏳' : '✗'
  );
  row.appendChild(el('div', {
    className: `check-icon ${iconClass}`,
    text: iconText,
  }));

  const body = el('div', { className: 'check-body' });
  body.appendChild(el('div', { className: 'check-name', text: check.name }));
  body.appendChild(el('div', { className: 'check-msg', text: check.message || '' }));
  if (check.fix_hint) {
    body.appendChild(el('div', { className: 'check-hint', text: '修复: ' + check.fix_hint }));
  }
  row.appendChild(body);

  return row;
}

async function runHealthCheck() {
  const body = $('#health-body');
  body.replaceChildren(el('div', {
    className: 'loader small',
    children: [el('div', { className: 'spinner' })],
  }));
  $('#health-summary').textContent = '检测中…';
  try {
    const r = await window.pywebview.api.check_health();
    const frag = document.createDocumentFragment();
    for (const c of r.checks || []) frag.appendChild(buildCheckRow(c));
    body.replaceChildren(frag);
    const total = (r.checks || []).length;
    const okN = (r.checks || []).filter((c) => c.ok).length;
    const warnN = (r.checks || []).filter((c) => c.severity === 'warn').length;
    const errN = (r.checks || []).filter((c) => c.severity === 'err').length;
    let summary = `${okN}/${total} 通过`;
    if (warnN) summary += ` · ${warnN} 警告`;
    if (errN) summary += ` · ${errN} 错误`;
    $('#health-summary').textContent = summary;
  } catch (err) {
    body.replaceChildren(el('div', {
      className: 'check-row',
      children: [
        el('div', { className: 'check-icon err', text: '✗' }),
        el('div', {
          className: 'check-body',
          children: [
            el('div', { className: 'check-name', text: '检测调用失败' }),
            el('div', { className: 'check-msg', text: err && err.message ? err.message : String(err) }),
          ],
        }),
      ],
    }));
    $('#health-summary').textContent = '失败';
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

$('#btn-health').addEventListener('click', openHealthModal);
$('#btn-health-close').addEventListener('click', closeHealthModal);
$('#btn-health-rerun').addEventListener('click', runHealthCheck);

$('#health-modal').addEventListener('click', (e) => {
  if (e.target === $('#health-modal')) closeHealthModal();
});

$('#btn-inject').addEventListener('click', openInjectModal);
$('#btn-inject-close').addEventListener('click', closeInjectModal);
$('#btn-inject-cancel').addEventListener('click', closeInjectModal);
$('#btn-inject-confirm').addEventListener('click', applyInject);

$('#inject-modal').addEventListener('click', (e) => {
  if (e.target === $('#inject-modal')) closeInjectModal();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!$('#health-modal').classList.contains('hidden')) closeHealthModal();
    if (!$('#inject-modal').classList.contains('hidden')) closeInjectModal();
  }
});
