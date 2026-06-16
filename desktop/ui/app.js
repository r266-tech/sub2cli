// desktop/ui/app.js — frontend state + bridge to pywebview.api
// 全部 DOM 构造走 createElement + textContent, 避免 innerHTML 的 XSS 面.

const $ = (sel) => document.querySelector(sel);

const state = {
  bootstrapped: false,
  endpoints: [],
  groups: [],
  keys: [],
  subscriptions: [],
  defaultKey: null,
  codexKey: null,
  defaultEndpoint: null,
  pingResults: {}, // url → {ok, latency_ms, summary} | {running:true}
  groupResults: {}, // group_id → {modelName:{...}}
  groupSelected: {}, // group_id → boolean (checkbox state for batch test)
  models: [],
  groupModels: {}, // group_id -> model names returned while the test key is in that group
  modelError: null,
  groupModelColumns: [], // model names selected for group test columns
  groupTestRunning: false,
  relays: [],
  lastDomain: null,  // current relay domain, for modal hints
  currentEmail: null,
  viewMode: 'relay',
  codexAccounts: [],
  currentCodexAccount: null,
  customApis: [],          // [{id, name, base_url, key_masked, model_columns, is_current}]
  currentCustomApi: null,  // full object of the selected custom API
  customApiModels: [],     // available models from refresh_custom_api_models
  customApiColumns: [],    // model names the user picked to test
  customApiResults: {},    // model -> {ok, latency_ms, status, running}
  customApiModelError: null,
  routePools: [],
  currentRoutePoolId: null,
  routePoolDraft: null,
  routePoolCandidates: { relay_keys: [], relay_endpoints: [], custom_apis: [] },
  routePoolStatus: null,
  routePoolMessage: '',
  routePoolStatusRefreshInFlight: false,
  routePoolLastStatusAt: 0,
  routePoolRunning: false,
  routePoolEditorOpen: false,
  currentInjectTarget: 'relay',
  configTargetRunning: false,
  retryRelayDomain: null,
  dragSorting: false,
  lastInjectBackup: null,
  lastInjectChanges: null,
  keySecret: null,
};

const PROJECT_URL = 'https://github.com/r266-tech/sub2cli';
const RELAY_ORDER_KEY = 'sub2cli.sidebar.relayOrder';
const CODEX_ORDER_KEY = 'sub2cli.sidebar.codexOrder';
let latestUpdateInfo = null;
let routePoolStatusTimer = null;
let routePoolContextRouteId = null;

// ---- screen routing ----

function showScreen(id) {
  for (const s of ['loading', 'error', 'dashboard']) {
    $(`#${s}`).classList.toggle('hidden', s !== id);
  }
}

// Intentional no-ops: the v2 dark UI has no status-bar / brand-subtitle element
// (per the selected design). Kept as stubs so the ~80 existing call sites stay
// valid without scattering progress text into a non-existent slot. Do NOT treat
// as "unimplemented" — wiring these needs a real UI element, which is a design
// change (route via AGENTS.md gemini flow), not a bug fix.
function setStatus(_text, _cls = '') {}

function setBrandSubtitle(_text) {}

function showError(title, msg) {
  $('#error-title').textContent = title;
  $('#error-msg').textContent = msg || '(空)';
  showScreen('error');
}

function showLoginError(domain, fallbackMsg = '') {
  state.retryRelayDomain = domain || state.lastDomain || null;
  showError('请重新登录', fallbackMsg || `请先在浏览器登录 ${state.retryRelayDomain || '当前 relay'}`);
}

function openProjectPage() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
    window.pywebview.api.open_url(PROJECT_URL).catch(() => {});
  } else {
    window.open(PROJECT_URL, '_blank');
  }
}

// ---- formatting ----

function fmtMoney(v) {
  if (typeof v !== 'number') return '?';
  return '$' + v.toFixed(2);
}

function asNumber(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function fmtUsd(v) {
  const n = asNumber(v);
  if (n == null) return '—';
  return '$' + n.toFixed(2);
}

function fmtLatency(ms) {
  if (typeof ms !== 'number') return '?';
  return ms + 'ms';
}

function fmtRate(r) {
  if (r === null || r === undefined) return '?';
  return r + 'x';
}

function fmtAgo(iso) {
  if (!iso) return 'Updated —';
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return 'Updated —';
  const delta = Math.max(0, Date.now() - ts);
  const mins = Math.round(delta / 60000);
  if (mins < 1) return 'Updated just now';
  if (mins < 60) return `Updated ${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `Updated ${hours}h ago`;
  return `Updated ${Math.round(hours / 24)}d ago`;
}

function fmtDurationFromNow(ts) {
  if (!ts) return '—';
  const ms = (Number(ts) * 1000) - Date.now();
  if (!Number.isFinite(ms)) return '—';
  if (ms <= 0) return 'now';
  const totalMinutes = Math.round(ms / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const mins = totalMinutes % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

function fmtRemainingDays(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso) - Date.now();
  if (!Number.isFinite(ms)) return '—';
  if (ms <= 0) return '已过期';
  const days = Math.ceil(ms / 86400000);
  if (days >= 1) return `剩余 ${days} 天`;
  return '今天到期';
}

function leftPercent(used) {
  if (typeof used !== 'number') return '—';
  return `${Math.max(0, Math.round(100 - used))}%`;
}

// build a <span class="tag ..."> element for a probe/test result cell
function probeErrorLabel(result) {
  const status = result && result.status;
  if (status != null && status !== -1 && status !== 'err') return String(status);
  const summary = String((result && result.summary) || '').toLowerCase();
  if (summary.includes('readtimeout') || summary.includes('timeout') || summary.includes('timed out')) {
    return '超时';
  }
  if (summary.includes('connectionerror') || summary.includes('failed to establish') || summary.includes('connection aborted')) {
    return '网络';
  }
  if (summary.includes('name resolution') || summary.includes('dns') || summary.includes('nodename')) {
    return 'DNS';
  }
  if (summary.includes('ssl') || summary.includes('tls')) {
    return 'TLS';
  }
  return '请求失败';
}

function buildTag(result) {
  const span = document.createElement('span');
  span.className = 'tag';
  if (!result) {
    span.className = 'cell-dash';
    span.textContent = '—';
    return span;
  }
  if (result.running) {
    span.classList.add('running');
    span.textContent = '[RUN]';
    return span;
  }
  if (result.ok) {
    span.classList.add('ok');
    span.textContent = fmtLatency(result.latency_ms);
    return span;
  }
  span.classList.add('err');
  span.textContent = `[ERR] ${probeErrorLabel(result)}`;
  const detail = result.summary || (result.status != null ? `status=${result.status}` : '');
  if (detail) span.title = detail;
  return span;
}

function el(tag, opts = {}) {
  const e = document.createElement(tag);
  if (opts.className) e.className = opts.className;
  if (opts.text != null) e.textContent = String(opts.text);
  if (opts.children) for (const c of opts.children) if (c) e.appendChild(c);
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v == null || v === false) continue;
      e.setAttribute(k, v === true ? '' : v);
    }
  }
  if (opts.onClick) e.addEventListener('click', opts.onClick);
  return e;
}

function uniqueModels(items) {
  const result = [];
  const seen = new Set();
  for (const item of items || []) {
    const model = String(item || '').trim();
    if (!model || seen.has(model)) continue;
    result.push(model);
    seen.add(model);
  }
  return result;
}

function sidebarItemButton(text, { className = '', title = '', onClick } = {}) {
  const button = el('button', {
    className: `sidebar-item-mini ${className}`.trim(),
    text,
    attrs: { type: 'button', title, draggable: 'false' },
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (onClick) onClick(e);
    },
  });
  button.addEventListener('mousedown', (e) => e.stopPropagation());
  button.addEventListener('dragstart', (e) => e.preventDefault());
  return button;
}

function appendSidebarActions(item, buttons) {
  const actions = el('div', { className: 'sidebar-item-actions' });
  for (const button of buttons || []) actions.appendChild(button);
  item.appendChild(actions);
  return actions;
}

function suppressSidebarContextMenu(item) {
  item.addEventListener('contextmenu', (e) => e.preventDefault());
}

function normalizeGroupModelColumns(columns, models) {
  const result = uniqueModels(columns);
  if (result.length) return result;
  const available = uniqueModels(models);
  return available.slice(0, 2);
}

function modelOptionsForColumn(currentModel, columnIndex = -1) {
  const groupOptionLists = Object.values(state.groupModels || {});
  const opts = uniqueModels([...state.models, ...groupOptionLists.flat()]);
  const current = String(currentModel || '').trim();
  if (current && !opts.includes(current)) opts.unshift(current);
  const selectedElsewhere = new Set(
    state.groupModelColumns.filter((_, idx) => idx !== columnIndex)
  );
  return opts.filter((model) => model === current || !selectedElsewhere.has(model));
}

function setGroupModelColumns(columns, { persist = true } = {}) {
  state.groupModelColumns = uniqueModels(columns);
  for (const gid of Object.keys(state.groupResults)) {
    const row = state.groupResults[gid] || {};
    for (const model of Object.keys(row)) {
      if (!state.groupModelColumns.includes(model)) delete row[model];
    }
  }
  renderGroups(state.groups, [], state.defaultKey);
  if (persist) persistGroupModelColumns();
}

async function persistGroupModelColumns() {
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.save_group_model_columns) return;
  try {
    const r = await window.pywebview.api.save_group_model_columns(state.groupModelColumns);
    if (r && r.ok) {
      state.models = uniqueModels(r.models || state.models);
      state.groupModels = r.group_models || state.groupModels || {};
      state.modelError = r.model_error || null;
      state.groupModelColumns = normalizeGroupModelColumns(r.group_model_columns || state.groupModelColumns, state.models);
      renderGroups(state.groups, [], state.defaultKey);
    } else if (r && r.error) {
      setStatus(r.error, 'err');
    }
  } catch (err) {
    setStatus(err && err.message ? err.message : String(err), 'err');
  }
}

// ---- one-mode UI ----

function clearModeState() {
  document.body.classList.remove('simple-mode');
  try { localStorage.removeItem('sub2cli.simple_mode'); } catch {}
}

// ---- bootstrap ----

async function bootstrap(autoRecovered = false) {
  showScreen('loading');
  setStatus('连接中…');
  try {
    const data = await window.pywebview.api.bootstrap();
    if (!data.ok) {
      // auto-recovery: stored token failed → try re-reading fresh token from Edge CDP once
      if (data.needs_login && !autoRecovered) {
        setStatus('token 过期, 从 Edge 重读…', 'warn');
        try {
          const r = await window.pywebview.api.import_edge_account();
          if (r.ok) {
            updateAccountChip(r.email);
            return bootstrap(true);  // retry with fresh token
          }
        } catch (_) {}
      }
      if (data.needs_login) {
        showLoginError(data.domain, `请先在浏览器登录 ${data.domain || '当前 relay'}`);
      } else if (data.needs_setup) {
        state.retryRelayDomain = null;
        showError('尚未配置', data.error || '未知错误');
      } else {
        state.retryRelayDomain = null;
        showError('错误', data.error || '未知错误');
      }
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
    const data = (await window.pywebview.api.refresh()) || {};
    if (!data.ok) {
      setStatus('刷新失败', 'err');
      if (data.needs_login) {
        showLoginError(data.domain, `请先在浏览器登录 ${data.domain || '当前 relay'}`);
      } else {
        state.retryRelayDomain = null;
        showError('错误', data.error);
      }
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
  state.lastDomain = data.domain || data.site || null;
  state.endpoints = data.endpoints || [];
  // Reset batch-test checkbox selection on every (re)bootstrap. group ids are
  // server-assigned per relay and collide across relays (1,2,3…); carrying the
  // old map over a relay/account switch would leave rows pre-selected and let
  // 批量测试 fire a real group-switch against the WRONG relay's group. Batch
  // selection is an explicit per-session action, so starting unchecked is correct.
  state.groupSelected = {};
  state.groups = data.groups || [];
  state.keys = data.keys || [];
  state.subscriptions = data.subscriptions || [];
  state.defaultKey = data.default_key;
  state.codexKey = data.codex_key || null;
  state.defaultEndpoint = data.default_endpoint;
  state.models = uniqueModels(data.models || []);
  state.groupModels = data.group_models || {};
  state.modelError = data.model_error || null;
  state.groupModelColumns = normalizeGroupModelColumns(data.group_model_columns || [], state.models);
  setBrandSubtitle(data.site || data.domain || '');
  renderAccount(data);
  renderSubscriptions(state.subscriptions);
  renderKeys(state.keys, state.groups, data.default_key, state.codexKey);
  renderEndpoints(data.endpoints || [], data.default_endpoint);
  renderGroups(data.groups || [], data.keys || [], data.default_key);
  refreshSidebar();  // sidebar is independent of bootstrap data
  refreshCodexAccounts();
  refreshCustomApis();
  refreshRoutePools();
  updateContextLabels();
  updateAccountChip((data.user && data.user.email) || null);
}

function updateAccountChip(email) {
  state.currentEmail = email || null;
  $('#account-email').textContent = email || '未设';
  updateAccountDeleteButton(email || null);
  updateContextLabels();
}

function currentRelayLabel() {
  return (state.lastDomain || '').replace(/^https?:\/\//, '').replace(/\/$/, '') || '—';
}

function currentCodexLabel() {
  const acc = state.currentCodexAccount;
  if (!acc) return '未设置';
  return acc.email || acc.display_name || acc.slot || '未设置';
}

function updateContextLabels() {
  const relay = currentRelayLabel();
  const codex = currentCodexLabel();
  const popRelay = $('#pop-current-relay');
  const popCodex = $('#pop-current-codex');
  if (popRelay) popRelay.textContent = relay;
  if (popCodex) popCodex.textContent = codex;
  updateConfigChoiceMeta();
}

function syncCurrentRelaySummary(patch) {
  if (!state.lastDomain || !patch) return;
  state.relays = (state.relays || []).map((relay) => {
    if (!relay || relay.domain !== state.lastDomain) return relay;
    return { ...relay, ...patch, is_current: true };
  });
}

// ---- sidebar (multi-relay) ----

function readOrder(key) {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch (_) {
    return [];
  }
}

function writeOrder(key, values) {
  localStorage.setItem(key, JSON.stringify((values || []).filter(Boolean)));
}

function applyStoredOrder(items, key, getId) {
  const order = readOrder(key);
  if (!order.length) return items;
  const rank = new Map(order.map((id, index) => [id, index]));
  return [...items].sort((a, b) => {
    const ar = rank.has(getId(a)) ? rank.get(getId(a)) : Number.MAX_SAFE_INTEGER;
    const br = rank.has(getId(b)) ? rank.get(getId(b)) : Number.MAX_SAFE_INTEGER;
    if (ar !== br) return ar - br;
    return items.indexOf(a) - items.indexOf(b);
  });
}

function getDragAfterElement(container, y) {
  const items = [...container.querySelectorAll('.sortable-item:not(.dragging)')];
  return items.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) return { offset, element: child };
    return closest;
  }, { offset: Number.NEGATIVE_INFINITY, element: null }).element;
}

function installSortableList(container, options) {
  if (!container || container.dataset.sortableReady === '1') return;
  container.dataset.sortableReady = '1';
  container.addEventListener('dragover', (e) => {
    const dragging = container.querySelector('.sortable-item.dragging');
    if (!dragging) return;
    e.preventDefault();
    const after = getDragAfterElement(container, e.clientY);
    if (after == null) container.appendChild(dragging);
    else container.insertBefore(dragging, after);
  });
  container.addEventListener('drop', (e) => {
    const dragging = container.querySelector('.sortable-item.dragging');
    if (!dragging) return;
    e.preventDefault();
    const order = [...container.querySelectorAll('.sortable-item')]
      .map((item) => item.dataset.sortKey)
      .filter(Boolean);
    options.onOrder(order);
  });
}

function makeSortableItem(item, key) {
  item.classList.add('sortable-item');
  item.draggable = true;
  item.dataset.sortKey = key;
  item.addEventListener('dragstart', (e) => {
    state.dragSorting = true;
    item.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', key);
  });
  item.addEventListener('dragend', () => {
    item.classList.remove('dragging');
    setTimeout(() => { state.dragSorting = false; }, 120);
  });
}

async function refreshSidebar() {
  const list = $('#sidebar-list');
  try {
    const r = (await window.pywebview.api.list_relays_full()) || {};
    if (!r.ok) {
      list.replaceChildren(el('div', {
        className: 'muted small',
        text: r.error || '(空)',
      }));
      return;
    }
    const relays = applyStoredOrder(r.relays || [], RELAY_ORDER_KEY, (relay) => relay.domain);
    state.relays = relays;
    const frag = document.createDocumentFragment();
    for (const relay of relays) {
      const item = el('div', { className: 'relay-item' + (relay.is_current ? ' active' : '') });
      suppressSidebarContextMenu(item);
      makeSortableItem(item, relay.domain);
      const info = el('div', { className: 'relay-info' });
      info.appendChild(el('span', { className: 'relay-domain', text: relay.site || relay.domain }));
      item.appendChild(info);
      appendSidebarActions(item, [
        sidebarItemButton('删除', {
          className: 'danger',
          title: '删除中转站',
          onClick: () => {
            if (confirm(`删除中转站 ${relay.site || relay.domain}?\n会清掉它在 Keychain 里的 token 和密码 (不可恢复).`)) {
              removeRelay(relay.domain);
            }
          },
        }),
      ]);
      if (relay.is_current) item.addEventListener('click', () => {
        if (!state.dragSorting) showRelayDashboard();
      });
      else item.addEventListener('click', () => {
        if (!state.dragSorting) switchRelay(relay.domain);
      });
      frag.appendChild(item);
    }
    if (!r.relays.length) {
      frag.appendChild(el('div', { className: 'muted small', text: '(尚无 relay; 跑 ./sub2cli 走 wizard)' }));
    }
    list.replaceChildren(frag);
    installSortableList(list, {
      onOrder: (order) => {
        writeOrder(RELAY_ORDER_KEY, order);
        setStatus('✓ 已保存中转列表顺序', 'ok');
      },
    });
  } catch (err) {
    list.replaceChildren(el('div', { className: 'muted small', text: '错: ' + String(err) }));
  }
}

async function refreshCodexAccounts() {
  const list = $('#codex-account-list');
  if (!list) return;
  try {
    const r = (await window.pywebview.api.list_codex_accounts()) || {};
    if (!r.ok) {
      list.replaceChildren(el('div', { className: 'muted small', text: r.error || '(空)' }));
      state.codexAccounts = [];
      state.currentCodexAccount = null;
      updateContextLabels();
      return;
    }
    state.codexAccounts = applyStoredOrder(r.accounts || [], CODEX_ORDER_KEY, codexAccountKey);
    state.currentCodexAccount = r.current_official || state.codexAccounts[0] || null;
    renderCodexAccountList();
    updateContextLabels();
    if (state.viewMode === 'official') renderOfficialDashboard(state.currentCodexAccount);
  } catch (err) {
    list.replaceChildren(el('div', { className: 'muted small', text: '错: ' + String(err) }));
  }
}

function renderCodexAccountList() {
  const list = $('#codex-account-list');
  const frag = document.createDocumentFragment();
  state.codexAccounts = applyStoredOrder(state.codexAccounts, CODEX_ORDER_KEY, codexAccountKey);
  for (const acc of state.codexAccounts) {
    const active = state.currentCodexAccount && state.currentCodexAccount.slot === acc.slot;
    const item = el('div', { className: 'codex-account-item' + (active ? ' active' : '') });
    suppressSidebarContextMenu(item);
    makeSortableItem(item, codexAccountKey(acc));
    const main = el('div', { className: 'codex-account-main' });
    main.appendChild(el('div', { className: 'codex-account-name', text: codexAccountName(acc) }));
    item.appendChild(main);
    appendSidebarActions(item, [
      sidebarItemButton('重登', {
        title: '重新登录此官方账号',
        onClick: () => openReloginCodexAccountModal(acc),
      }),
      sidebarItemButton('删除', {
        className: 'danger',
        title: '删除此官方账号槽位',
        onClick: () => removeCodexAccount(acc),
      }),
    ]);
    item.addEventListener('click', () => {
      if (!state.dragSorting) selectCodexAccount(acc.slot);
    });
    frag.appendChild(item);
  }
  if (!state.codexAccounts.length) {
    frag.appendChild(el('div', {
      className: 'muted small',
      text: '尚无官方账号; 点标题右侧 添加',
    }));
  }
  list.replaceChildren(frag);
  installSortableList(list, {
    onOrder: (order) => {
      writeOrder(CODEX_ORDER_KEY, order);
      state.codexAccounts = applyStoredOrder(state.codexAccounts, CODEX_ORDER_KEY, codexAccountKey);
      setStatus('✓ 已保存官号列表顺序', 'ok');
    },
  });
}

function codexAccountKey(acc) {
  return (acc && (acc.slot || acc.identity_key || acc.email || acc.display_name)) || '';
}

function codexAccountName(acc) {
  if (!acc) return '未命名账号';
  return acc.email || acc.display_name || acc.slot || '未命名账号';
}

function codexPlanTitle(acc) {
  if (!acc) return '—';
  const plan = acc.plan_label || 'Codex';
  const slot = (acc.slot || '').trim();
  if (slot && !['local', 'default', 'official'].includes(slot.toLowerCase())) {
    return `${plan}.${slot}`;
  }
  return plan;
}

function mergeCodexAccountsPreserveOrder(nextAccounts) {
  const incoming = nextAccounts || [];
  if (!state.codexAccounts.length) return applyStoredOrder(incoming, CODEX_ORDER_KEY, codexAccountKey);
  const byKey = new Map(incoming.map((acc) => [codexAccountKey(acc), acc]));
  const used = new Set();
  const merged = state.codexAccounts.map((oldAcc) => {
    const key = codexAccountKey(oldAcc);
    const next = byKey.get(key);
    if (next) {
      used.add(key);
      return next;
    }
    return oldAcc;
  });
  for (const acc of incoming) {
    const key = codexAccountKey(acc);
    if (!used.has(key)) merged.push(acc);
  }
  return applyStoredOrder(merged, CODEX_ORDER_KEY, codexAccountKey);
}

async function selectCodexAccount(slot) {
  const local = state.codexAccounts.find((a) => a.slot === slot);
  if (local) state.currentCodexAccount = local;
  state.viewMode = 'official';
  renderMainMode();
  renderCodexAccountList();
  updateContextLabels();
  renderOfficialDashboard(state.currentCodexAccount);
  try {
    const r = (await window.pywebview.api.select_codex_account(slot)) || {};
    if (r.ok) {
      state.codexAccounts = mergeCodexAccountsPreserveOrder(r.accounts);
      state.currentCodexAccount = r.current_official || state.currentCodexAccount;
      renderCodexAccountList();
      updateContextLabels();
      renderOfficialDashboard(state.currentCodexAccount);
    }
  } catch (_) {}
}

function showRelayDashboard() {
  state.viewMode = 'relay';
  renderMainMode();
  stopRoutePoolStatusRefresh();
}

function showRoutePoolDashboard() {
  state.viewMode = 'pool';
  renderMainMode();
  startRoutePoolStatusRefresh();
  refreshRoutePools();
}

function renderMainMode() {
  const relay = $('#relay-dashboard');
  const official = $('#official-dashboard');
  const custom = $('#custom-api-dashboard');
  const pool = $('#route-pool-dashboard');
  if (!relay || !official) return;
  relay.classList.toggle('hidden', state.viewMode !== 'relay');
  official.classList.toggle('hidden', state.viewMode !== 'official');
  if (custom) custom.classList.toggle('hidden', state.viewMode !== 'custom');
  if (pool) pool.classList.toggle('hidden', state.viewMode !== 'pool');
  const poolBtn = $('#btn-manage-pool');
  if (poolBtn) poolBtn.classList.toggle('active', state.viewMode === 'pool');
  if (state.viewMode === 'pool') startRoutePoolStatusRefresh();
  else stopRoutePoolStatusRefresh();
}

// ---- route pools ----

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value == null ? null : value));
}

function defaultRoutePoolPolicy() {
  return {
    fail_consecutive: 2,
    recovery_successes: 2,
    cooldown_seconds: [60, 120, 300],
    min_dwell_seconds: 60,
    probe_interval_seconds: 90,
    current_probe_interval_seconds: 0,
    rate_limit_cooldown_seconds: 120,
  };
}

function newRoutePoolDraft() {
  return {
    id: 'default-pool',
    name: '连接池',
    description: '',
    model: '',
    policy: defaultRoutePoolPolicy(),
    routes: [],
  };
}

function activeRoutePoolDraft() {
  if (!state.routePoolDraft) state.routePoolDraft = newRoutePoolDraft();
  if (!state.routePoolDraft.policy) state.routePoolDraft.policy = defaultRoutePoolPolicy();
  if (!Array.isArray(state.routePoolDraft.routes)) state.routePoolDraft.routes = [];
  return state.routePoolDraft;
}

function routePoolNextPriority() {
  const draft = activeRoutePoolDraft();
  const max = draft.routes.reduce((m, route) => Math.max(m, Number(route.priority) || 0), 0);
  return max + 10;
}

function renumberRoutePoolRoutes(routes) {
  (routes || []).forEach((route, index) => {
    route.priority = (index + 1) * 10;
  });
}

function installRoutePoolTableDnD(tbody) {
  if (!tbody || tbody.dataset.routePoolSortableReady === '1') return;
  tbody.dataset.routePoolSortableReady = '1';
  tbody.addEventListener('dragover', (e) => {
    const dragging = tbody.querySelector('tr.pool-route-row.dragging');
    if (!dragging) return;
    e.preventDefault();
    const rows = [...tbody.querySelectorAll('tr.pool-route-row:not(.dragging)')];
    const after = rows.reduce((closest, child) => {
      const box = child.getBoundingClientRect();
      const offset = e.clientY - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) return { offset, element: child };
      return closest;
    }, { offset: Number.NEGATIVE_INFINITY, element: null }).element;
    if (after == null) tbody.appendChild(dragging);
    else tbody.insertBefore(dragging, after);
  });
  tbody.addEventListener('drop', (e) => {
    const dragging = tbody.querySelector('tr.pool-route-row.dragging');
    if (!dragging) return;
    e.preventDefault();
    const draft = activeRoutePoolDraft();
    const byId = new Map(draft.routes.map((route) => [route.id, route]));
    const ordered = [...tbody.querySelectorAll('tr.pool-route-row')]
      .map((row) => byId.get(row.dataset.routeId))
      .filter(Boolean);
    if (ordered.length !== draft.routes.length) return;
    draft.routes = ordered;
    renumberRoutePoolRoutes(draft.routes);
    renderRoutePoolDashboard();
  });
}

function routePoolRouteId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.max(1, activeRoutePoolDraft().routes.length + 1)}`;
}

function routePoolEndpointRows() {
  const candidates = state.routePoolCandidates || {};
  const endpoints = [...(candidates.relay_endpoints || [])];
  const current = candidates.current_endpoint_url || (state.defaultEndpoint && state.defaultEndpoint.endpoint);
  if (current && !endpoints.some((ep) => ep.endpoint === current)) {
    endpoints.unshift({
      name: candidates.current_endpoint_name || '默认',
      endpoint: current,
    });
  }
  return endpoints;
}

function routePoolRelaySources() {
  const candidates = state.routePoolCandidates || {};
  const sources = Array.isArray(candidates.relay_sources) ? candidates.relay_sources : [];
  if (sources.length) return sources;
  const fallbackDomain = candidates.relay_domain || state.lastDomain || '';
  if (!fallbackDomain) return [];
  return [{
    domain: fallbackDomain,
    site: fallbackDomain.replace(/^https?:\/\//, '').replace(/\/$/, ''),
    is_current: true,
    keys: candidates.relay_keys || [],
    endpoints: routePoolEndpointRows(),
  }];
}

function selectedRoutePoolRelaySource() {
  const sources = routePoolRelaySources();
  const selected = selectValue('#route-pool-relay-domain')
    || (state.routePoolCandidates && state.routePoolCandidates.relay_domain)
    || state.lastDomain
    || '';
  return sources.find((source) => source.domain === selected) || sources[0] || null;
}

function upsertRoutePoolRelaySource(source) {
  if (!source || !source.domain) return;
  const candidates = state.routePoolCandidates || {};
  const sources = Array.isArray(candidates.relay_sources) ? [...candidates.relay_sources] : [];
  const idx = sources.findIndex((item) => item.domain === source.domain);
  if (idx >= 0) sources[idx] = { ...sources[idx], ...source };
  else sources.push(source);
  candidates.relay_sources = sources;
  state.routePoolCandidates = candidates;
}

async function loadRoutePoolRelaySource(domain) {
  const relaySource = routePoolRelaySources().find((source) => source.domain === domain);
  if (!relaySource || relaySource.loaded || relaySource.loading) return;
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.route_pool_relay_source) return;
  upsertRoutePoolRelaySource({ ...relaySource, loading: true, error: '加载中...' });
  renderRoutePoolSourceControls();
  try {
    const r = await window.pywebview.api.route_pool_relay_source(relaySource.domain);
    if (r && r.source) {
      upsertRoutePoolRelaySource({ ...r.source, loading: false });
    } else {
      upsertRoutePoolRelaySource({
        ...relaySource,
        loading: false,
        error: (r && r.error) || '中转站读取失败',
      });
    }
  } catch (err) {
    upsertRoutePoolRelaySource({
      ...relaySource,
      loading: false,
      error: err && err.message ? err.message : String(err),
    });
  }
  renderRoutePoolSourceControls();
}

async function loadSelectedRoutePoolRelaySource() {
  const relaySource = selectedRoutePoolRelaySource();
  if (!relaySource) return;
  await loadRoutePoolRelaySource(relaySource.domain);
}

function handleRoutePoolSourceTypeChange() {
  renderRoutePoolSourceControls();
  if ((selectValue('#route-pool-source-type') || 'custom') === 'relay') {
    loadSelectedRoutePoolRelaySource();
  }
}

function handleRoutePoolRelayDomainChange() {
  renderRoutePoolSourceControls();
  loadSelectedRoutePoolRelaySource();
}

function renderRoutePoolSelect() {
  const select = $('#route-pool-select');
  if (!select) return;
  const rows = (state.routePools || []).map((pool) => ({
    value: pool.id,
    label: pool.name || pool.id,
  }));
  setSelectOptions(select, rows, state.currentRoutePoolId || '', '未保存');
}

function renderRoutePoolSourceControls() {
  const candidates = state.routePoolCandidates || {};
  const sourceType = selectValue('#route-pool-source-type') || 'custom';
  const relaySources = routePoolRelaySources();
  const currentRelayDomain = selectValue('#route-pool-relay-domain');
  const preferredRelay = candidates.relay_domain || state.lastDomain || '';
  const selectedRelayDomain = relaySources.some((source) => source.domain === currentRelayDomain)
    ? currentRelayDomain
    : preferredRelay;
  const relayDomainRows = relaySources.map((source) => ({
    value: source.domain,
    label: `${source.site || source.domain}${source.is_current ? ' · 当前' : ''}`,
  }));
  setSelectOptions($('#route-pool-relay-domain'), relayDomainRows, selectedRelayDomain, '无中转站');

  const relaySource = selectedRoutePoolRelaySource();
  const relayRows = ((relaySource && relaySource.keys) || []).map((key) => ({
    value: String(key.id),
    label: `${key.name || key.id} · ${key.group_name || '未分组'} (${fmtRate(key.group_rate)})`,
  }));
  const relayEmpty = relaySource && relaySource.loading
    ? '加载中...'
    : relaySource && relaySource.error
      ? relaySource.error
      : relaySource && !relaySource.loaded
        ? '选择后加载 key'
        : '无 relay key';
  setSelectOptions($('#route-pool-relay-key'), relayRows, '', relayEmpty);

  const endpointRows = ((relaySource && relaySource.endpoints) || []).map((ep) => ({
    value: ep.endpoint,
    label: `${ep.name || 'endpoint'} · ${ep.endpoint}`,
  }));
  const selectedEndpoint = relaySource && relaySource.current_endpoint_url
    ? relaySource.current_endpoint_url
    : (state.defaultEndpoint && state.defaultEndpoint.endpoint) || '';
  const endpointEmpty = relaySource && relaySource.loading
    ? '加载中...'
    : relaySource && relaySource.error
      ? relaySource.error
      : relaySource && !relaySource.loaded
        ? '选择后加载 endpoint'
        : '无 endpoint';
  setSelectOptions($('#route-pool-relay-endpoint'), endpointRows, selectedEndpoint, endpointEmpty);

  const customRows = (candidates.custom_apis || []).map((api) => ({
    value: api.id,
    label: customApiName(api),
  }));
  setSelectOptions($('#route-pool-custom-api'), customRows, state.currentCustomApi ? state.currentCustomApi.id : '', '无自定义 API');

  const relayDomain = $('#route-pool-relay-domain');
  const relayKey = $('#route-pool-relay-key');
  const relayEndpoint = $('#route-pool-relay-endpoint');
  const customApi = $('#route-pool-custom-api');
  if (relayDomain) relayDomain.classList.toggle('hidden', sourceType !== 'relay');
  if (relayKey) relayKey.classList.toggle('hidden', sourceType !== 'relay');
  if (relayEndpoint) relayEndpoint.classList.toggle('hidden', sourceType !== 'relay');
  if (customApi) customApi.classList.toggle('hidden', sourceType !== 'custom');
}

function renderRoutePoolEditor() {
  const editor = $('#route-pool-editor');
  if (editor) editor.classList.toggle('hidden', !state.routePoolEditorOpen);
  const toggle = $('#btn-route-pool-toggle-add');
  if (toggle) toggle.textContent = state.routePoolEditorOpen ? '取消' : '添加';
  renderRoutePoolSourceControls();
}

function toggleRoutePoolEditor() {
  state.routePoolEditorOpen = !state.routePoolEditorOpen;
  renderRoutePoolEditor();
}

function routePoolRuntimeSnapshot() {
  const status = state.routePoolStatus;
  if (!status || !status.ok) return null;
  const snap = status.snapshot || null;
  if (!snap || !snap.ok) return null;
  return snap;
}

function routePoolRuntimeState(route) {
  const snap = routePoolRuntimeSnapshot();
  const states = (snap && snap.states) || {};
  const id = String(route && route.id ? route.id : '');
  return {
    currentRoute: snap ? String(snap.current_route || '') : '',
    state: states[id] || null,
    snapshot: snap,
  };
}

function routePoolRuntimeStatus(route) {
  const runtime = routePoolRuntimeState(route);
  const current = runtime.currentRoute && route && String(route.id) === runtime.currentRoute;
  const routeState = runtime.state || {};
  const cooldown = Number(routeState.cooldown_remaining || 0);
  const failures = Number(routeState.failures || 0);
  const probeSuccesses = Number(routeState.probe_successes || 0);
  const blocked = !!routeState.blocked;
  const lastStatus = routeState.last_status;
  const lastError = routeState.last_error || '';
  let key = 'ready';
  let label = 'READY';
  let detail = '可用，未发现故障';
  let symbol = '○';

  if (!runtime.snapshot) {
    key = 'unknown';
    label = 'UNKNOWN';
    detail = '本地代理状态未读取';
    symbol = '?';
  } else if (current) {
    key = 'active';
    label = 'ACTIVE';
    detail = '当前正在使用';
    symbol = '●';
  } else if (blocked) {
    key = 'blocked';
    label = 'BLOCKED';
    detail = '认证或权限阻断';
    symbol = '▲';
  } else if (cooldown > 0) {
    key = 'cooldown';
    label = `COOL ${cooldown}s`;
    detail = failures ? `连续失败 ${failures} 次` : '冷却中';
    symbol = '↻';
  } else if (probeSuccesses > 0) {
    key = 'recovering';
    label = 'RECOVERING';
    detail = `恢复探测 ${probeSuccesses} 次通过`;
    symbol = '↻';
  } else if (failures > 0 || lastStatus == null && lastError) {
    key = 'failed';
    label = failures ? `FAIL ${failures}` : 'FAILED';
    detail = lastError || '最近请求失败';
    symbol = '▲';
  } else if (lastStatus === 200) {
    key = 'healthy';
    label = 'HEALTHY';
    detail = '最近请求成功';
    symbol = '●';
  }

  return {
    key,
    label,
    detail,
    symbol,
    current,
    cooldown,
    failures,
    blocked,
    probeSuccesses,
    lastStatus,
    lastError,
  };
}

function routePoolStatusCounts(routes) {
  const counts = { active: 0, healthy: 0, degraded: 0, failed: 0 };
  (routes || []).forEach((route) => {
    const status = routePoolRuntimeStatus(route);
    if (status.key === 'active') counts.active += 1;
    if (['active', 'healthy', 'ready'].includes(status.key)) counts.healthy += 1;
    if (['cooldown', 'recovering'].includes(status.key)) counts.degraded += 1;
    if (['failed', 'blocked'].includes(status.key)) counts.failed += 1;
  });
  return counts;
}

function routePoolRouteDisplay(route, idx) {
  const source = route.source_type === 'custom'
    ? route.custom_api_name || route.label || route.custom_api_id || `API ${idx + 1}`
    : route.label || route.key_name || route.relay_site || route.relay_domain || `route ${idx + 1}`;
  const base = route.base_url || route.relay_domain || '';
  const group = route.source_type === 'custom'
    ? '默认'
    : route.group_name || route.key_name || '未分组';
  return { source, base, group };
}

function routePoolHealthText(status) {
  if (status.key === 'unknown') return '等待 poolz';
  if (status.current) return 'current';
  if (status.blocked) return 'blocked 24h';
  if (status.cooldown) return `cooldown ${status.cooldown}s`;
  if (status.probeSuccesses) return `probe ${status.probeSuccesses}`;
  if (status.failures) return `failures ${status.failures}`;
  if (status.lastStatus) return `HTTP ${status.lastStatus}`;
  return 'standby';
}

function routePoolStateChipText(status) {
  const code = {
    active: 'ACTIVE',
    healthy: 'OK',
    ready: 'READY',
    recovering: 'REC',
    cooldown: 'COOL',
    blocked: 'BLOCK',
    failed: 'FAIL',
    unknown: 'UNK',
  }[status.key] || status.label;
  let metric = routePoolHealthText(status);
  if (status.key === 'failed' && status.lastStatus) metric = String(status.lastStatus);
  if (status.key === 'cooldown' && status.cooldown) metric = `${status.cooldown}s`;
  if (status.key === 'recovering' && status.probeSuccesses) metric = `probe ${status.probeSuccesses}`;
  return `${code} · ${metric}`;
}

function formatRoutePoolUptime(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (total < 60) return `${Math.round(total)}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 48) return restMinutes ? `${hours}h ${restMinutes}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}d ${restHours}h` : `${days}d`;
}

function renderRoutePoolHud() {
  const draft = activeRoutePoolDraft();
  const routes = draft.routes || [];
  const snap = routePoolRuntimeSnapshot();
  const activeId = snap && snap.current_route ? String(snap.current_route) : '';
  const activeIdx = routes.findIndex((route) => String(route.id) === activeId);
  const activeRoute = activeIdx >= 0 ? routes[activeIdx] : null;
  const activeStatus = activeRoute ? routePoolRuntimeStatus(activeRoute) : null;
  const activeDisplay = activeRoute ? routePoolRouteDisplay(activeRoute, activeIdx) : null;
  const counters = routePoolStatusCounts(routes);

  const routeNode = $('#route-pool-active-route');
  const detailNode = $('#route-pool-active-detail');
  if (routeNode) {
    routeNode.textContent = activeRoute
      ? `${activeStatus.symbol} ${activeDisplay.source}`
      : (snap ? '无 active route' : '本地代理未报告 active');
    routeNode.className = `pool-hud-route ${activeStatus ? 'state-' + activeStatus.key : 'state-unknown'}`;
  }
  if (detailNode) {
    const statusText = activeStatus ? activeStatus.label : (snap ? 'NO ACTIVE' : 'UNAVAILABLE');
    const routeDetail = activeDisplay && activeDisplay.base ? ` · ${activeDisplay.base}` : '';
    const proxyDetail = snap && snap.pid ? ` · pid ${snap.pid} · up ${formatRoutePoolUptime(snap.uptime_seconds)}` : '';
    const stale = state.routePoolLastStatusAt ? ` · ${Math.max(0, Math.round((Date.now() - state.routePoolLastStatusAt) / 1000))}s ago` : '';
    detailNode.textContent = `${statusText}${routeDetail}${proxyDetail}${stale}`;
  }

  const setCount = (id, value) => {
    const node = $(id);
    if (node) node.textContent = String(value);
  };
  setCount('#route-pool-count-active', counters.active);
  setCount('#route-pool-count-healthy', counters.healthy);
  setCount('#route-pool-count-degraded', counters.degraded);
  setCount('#route-pool-count-failed', counters.failed);
}

function routePoolStatusText() {
  const status = state.routePoolStatus;
  const routeCount = activeRoutePoolDraft().routes.length;
  const logs = status && Array.isArray(status.logs)
    ? status.logs.filter((line) => line && isRoutePoolEventLog(line))
    : [];
  if (logs.length) return logs.slice(-120).join('\n');
  if (!status) return '[INFO] 连接池日志未读取';
  if (!status.ok) {
    const error = status.error || '';
    if (/Connection refused|Errno 61|poolz unavailable|urlopen error/i.test(error)) {
      return routeCount
        ? '[INFO] 连接池代理未启动\n点击「配置到 Codex」后会启动本地代理。'
        : '[INFO] 连接池未启用\n先添加 route，再保存并配置到 Codex。';
    }
    return `[WARN] 连接池状态不可读\n${error || 'local proxy unavailable'}`;
  }
  const snap = status.snapshot || {};
  if (!snap.ok) return `[WARN] ${snap.error || '当前 Codex 未切到连接池'}`;
  const states = snap.states || {};
  const lines = [`[READY] active=${snap.current_route || '(none)'}`];
  for (const [id, route] of Object.entries(states)) {
    const blocked = route.blocked ? 'blocked' : 'open';
    const cooldown = route.cooldown_remaining ? ` cooldown=${route.cooldown_remaining}s` : '';
    const failures = route.failures ? ` failures=${route.failures}` : '';
    lines.push(`[${blocked.toUpperCase()}] ${id}${failures}${cooldown}`);
  }
  return lines.join('\n');
}

function isRoutePoolEventLog(line) {
  return /\bpool (route|monitor)\b/i.test(String(line || ''));
}

function renderRoutePoolStatus(extraText) {
  if (extraText !== undefined) state.routePoolMessage = extraText || '';
  const node = $('#route-pool-status');
  if (!node) return;
  node.textContent = state.routePoolMessage || routePoolStatusText();
  renderRoutePoolHud();
}

function routePoolSourceValue(type, id) {
  return `${type}:${encodeURIComponent(String(id || ''))}`;
}

function parseRoutePoolSourceValue(value) {
  const raw = String(value || '');
  const sep = raw.indexOf(':');
  if (sep < 0) return { type: '', id: '' };
  return {
    type: raw.slice(0, sep),
    id: decodeURIComponent(raw.slice(sep + 1)),
  };
}

function routePoolRouteSourceValue(route) {
  if (route.source_type === 'custom') return routePoolSourceValue('custom', route.custom_api_id);
  return routePoolSourceValue('relay', route.relay_domain || '');
}

function routePoolRelaySourceByDomain(domain) {
  return routePoolRelaySources().find((source) => source.domain === domain) || null;
}

function routePoolCustomApiById(id) {
  const candidates = state.routePoolCandidates || {};
  return (candidates.custom_apis || []).find((api) => api.id === id) || null;
}

function routePoolSourceRows(route) {
  const rows = [];
  const seen = new Set();
  for (const source of routePoolRelaySources()) {
    const value = routePoolSourceValue('relay', source.domain);
    seen.add(value);
    rows.push({
      value,
      label: `中转 · ${source.site || source.domain}${source.is_current ? ' · 当前' : ''}`,
    });
  }
  const candidates = state.routePoolCandidates || {};
  for (const api of (candidates.custom_apis || [])) {
    const value = routePoolSourceValue('custom', api.id);
    seen.add(value);
    rows.push({ value, label: `API · ${customApiName(api)}` });
  }
  const current = routePoolRouteSourceValue(route);
  if (current && !seen.has(current)) {
    rows.unshift({
      value: current,
      label: route.source_type === 'custom'
        ? `API · ${route.custom_api_name || route.label || route.custom_api_id || '未知'}`
        : `中转 · ${route.relay_site || route.relay_domain || route.label || '未知'}`,
    });
  }
  return rows;
}

function routePoolEndpointForSource(source, route = {}) {
  const endpoints = (source && source.endpoints) || [];
  return endpoints.find((ep) => ep.endpoint === route.base_url)
    || endpoints.find((ep) => ep.endpoint === source.current_endpoint_url)
    || endpoints[0]
    || null;
}

function applyRelayKeyToRoute(route, source, key) {
  if (!route || !source || !key) return;
  const endpoint = routePoolEndpointForSource(source, route);
  const relayLabel = source.site || source.domain || route.relay_domain || '中转';
  route.source_type = 'relay';
  route.relay_domain = source.domain || route.relay_domain || '';
  route.relay_site = source.site || route.relay_site || '';
  route.key_id = key.id;
  route.key_name = key.name || String(key.id);
  route.key_masked = key.key_masked || '';
  route.group_id = key.group_id;
  route.group_name = key.group_name || '';
  route.group_rate = key.group_rate;
  route.endpoint_name = (endpoint && endpoint.name) || route.endpoint_name || '默认';
  route.base_url = (endpoint && endpoint.endpoint) || route.base_url || '';
  route.protocol = 'responses';
  route.model = route.model || state.groupModelColumns[0] || state.models[0] || '';
  route.label = `${relayLabel} · ${route.key_name} · ${route.group_name || '未分组'}`;
}

function applyRelaySourceDefaultsToRoute(route, source) {
  const key = ((source && source.keys) || []).find((item) => String(item.id) === String(route.key_id))
    || ((source && source.keys) || [])[0];
  if (key) applyRelayKeyToRoute(route, source, key);
}

function applyCustomApiToRoute(route, api) {
  if (!route || !api) return;
  route.source_type = 'custom';
  route.custom_api_id = api.id;
  route.custom_api_name = customApiName(api);
  route.base_url = api.base_url || '';
  route.protocol = 'chat';
  route.model = (api.model_columns && api.model_columns[0]) || '';
  route.label = customApiName(api);
  route.relay_domain = '';
  route.relay_site = '';
  route.key_id = '';
  route.key_name = '';
  route.group_id = '';
  route.group_name = '';
  route.group_rate = null;
}

async function updateRoutePoolRouteSource(route, value) {
  const parsed = parseRoutePoolSourceValue(value);
  if (parsed.type === 'custom') {
    const api = routePoolCustomApiById(parsed.id);
    if (api) applyCustomApiToRoute(route, api);
    renderRoutePoolDashboard();
    return;
  }
  if (parsed.type !== 'relay') return;
  const source = routePoolRelaySourceByDomain(parsed.id);
  route.source_type = 'relay';
  route.relay_domain = parsed.id;
  route.relay_site = source ? source.site || '' : '';
  route.protocol = 'responses';
  route.custom_api_id = '';
  route.custom_api_name = '';
  if (source && source.loaded) {
    applyRelaySourceDefaultsToRoute(route, source);
    renderRoutePoolDashboard();
    return;
  }
  route.key_id = '';
  route.key_name = '';
  route.group_id = '';
  route.group_name = '';
  route.group_rate = null;
  route.base_url = '';
  route.label = `${route.relay_site || route.relay_domain || '中转'} · 未加载`;
  renderRoutePoolDashboard();
  await loadRoutePoolRelaySource(parsed.id);
  const currentDraft = activeRoutePoolDraft();
  const loaded = routePoolRelaySourceByDomain(parsed.id);
  if (currentDraft.routes.includes(route) && loaded && loaded.loaded) {
    applyRelaySourceDefaultsToRoute(route, loaded);
    renderRoutePoolDashboard();
  }
}

function routePoolGroupRows(route) {
  if (route.source_type === 'custom') return [{ value: 'default', label: '默认' }];
  const source = routePoolRelaySourceByDomain(route.relay_domain || '');
  const rows = ((source && source.keys) || []).map((key) => ({
    value: String(key.id),
    label: `${key.group_name || '未分组'} · ${key.name || key.id} (${fmtRate(key.group_rate)})`,
  }));
  const current = route.key_id == null ? '' : String(route.key_id);
  if (current && !rows.some((row) => row.value === current)) {
    rows.unshift({
      value: current,
      label: `${route.group_name || '未分组'} · ${route.key_name || current} (${fmtRate(route.group_rate)})`,
    });
  }
  return rows;
}

function updateRoutePoolRouteGroup(route, value) {
  if (route.source_type !== 'relay') return;
  const source = routePoolRelaySourceByDomain(route.relay_domain || '');
  const key = ((source && source.keys) || []).find((item) => String(item.id) === String(value));
  if (!key) return;
  applyRelayKeyToRoute(route, source, key);
  renderRoutePoolDashboard();
}

function closeRoutePoolContextMenu() {
  const menu = $('#route-pool-context-menu');
  if (menu) menu.classList.add('hidden');
  document.querySelectorAll('.pool-route-row.context-open').forEach((row) => {
    row.classList.remove('context-open');
  });
  routePoolContextRouteId = null;
}

function openRoutePoolContextMenu(event, route) {
  const menu = $('#route-pool-context-menu');
  if (!menu || !route) return;
  event.preventDefault();
  closeRoutePoolContextMenu();
  routePoolContextRouteId = route.id;
  const row = event.currentTarget;
  if (row) row.classList.add('context-open');
  menu.style.left = `${event.clientX}px`;
  menu.style.top = `${event.clientY}px`;
  menu.classList.remove('hidden');
}

function deleteRoutePoolContextRoute() {
  if (!routePoolContextRouteId) return;
  const draft = activeRoutePoolDraft();
  const idx = draft.routes.findIndex((route) => route.id === routePoolContextRouteId);
  closeRoutePoolContextMenu();
  if (idx < 0) return;
  draft.routes.splice(idx, 1);
  renumberRoutePoolRoutes(draft.routes);
  renderRoutePoolDashboard();
}

function renderRoutePoolRoutes() {
  const tbody = $('#route-pool-body');
  if (!tbody) return;
  const draft = activeRoutePoolDraft();
  installRoutePoolTableDnD(tbody);
  const frag = document.createDocumentFragment();
  draft.routes.forEach((route, idx) => {
    const runtimeStatus = routePoolRuntimeStatus(route);
    const display = routePoolRouteDisplay(route, idx);
    const tr = document.createElement('tr');
    tr.className = `pool-route-row pool-state-${runtimeStatus.key}${runtimeStatus.current ? ' current active-route' : ''}`;
    tr.draggable = true;
    tr.dataset.routeId = route.id;
    tr.dataset.routeState = runtimeStatus.key;
    tr.title = '右键删除 route';
    tr.addEventListener('contextmenu', (e) => openRoutePoolContextMenu(e, route));
    tr.addEventListener('dragstart', (e) => {
      state.dragSorting = true;
      tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', route.id || String(idx));
    });
    tr.addEventListener('dragend', () => {
      tr.classList.remove('dragging');
      setTimeout(() => { state.dragSorting = false; }, 120);
    });

    const stateBadge = el('div', {
      className: `pool-state-badge state-${runtimeStatus.key}`,
      attrs: {
        title: runtimeStatus.lastError || runtimeStatus.detail,
        'aria-label': `${runtimeStatus.label}: ${runtimeStatus.lastError || runtimeStatus.detail}`,
      },
      children: [
        el('span', { className: 'pool-state-symbol', text: runtimeStatus.symbol }),
        el('span', { className: 'pool-state-label', text: routePoolStateChipText(runtimeStatus) }),
      ],
    });
    tr.appendChild(el('td', {
      className: 'pool-state-cell',
      children: [stateBadge],
    }));

    const sourceSelect = el('select', {
      className: 'pool-route-select',
      attrs: {
        'aria-label': '切换 route 来源',
        title: `${display.source}${display.base ? `\n${display.base}` : ''}`,
      },
    });
    setSelectOptions(sourceSelect, routePoolSourceRows(route), routePoolRouteSourceValue(route), '无来源');
    sourceSelect.addEventListener('contextmenu', (e) => e.stopPropagation());
    sourceSelect.addEventListener('change', () => updateRoutePoolRouteSource(route, sourceSelect.value));
    tr.appendChild(el('td', {
      children: [sourceSelect],
    }));

    const groupSelect = el('select', {
      className: 'pool-route-select',
      attrs: {
        'aria-label': '切换 route 分组',
        title: display.group,
      },
    });
    const source = routePoolRelaySourceByDomain(route.relay_domain || '');
    if (route.source_type === 'relay' && source && !source.loaded && !source.loading) {
      loadRoutePoolRelaySource(source.domain).then(() => {
        if (state.viewMode === 'pool') renderRoutePoolDashboard();
      });
    }
    const groupEmpty = source && source.loading
      ? '加载中...'
      : source && source.error
        ? source.error
        : '无分组';
    setSelectOptions(
      groupSelect,
      routePoolGroupRows(route),
      route.key_id == null ? 'default' : String(route.key_id),
      groupEmpty,
    );
    groupSelect.disabled = route.source_type === 'custom' || groupSelect.disabled;
    groupSelect.addEventListener('contextmenu', (e) => e.stopPropagation());
    groupSelect.addEventListener('change', () => updateRoutePoolRouteGroup(route, groupSelect.value));
    tr.appendChild(el('td', {
      children: [groupSelect],
    }));

    frag.appendChild(tr);
  });

  if (!draft.routes.length) {
    frag.appendChild(el('tr', {
      children: [el('td', {
        className: 'muted small',
        text: '还没有 route，点击「添加」后选择 API 或中转。',
        attrs: { colspan: '3' },
      })],
    }));
  }
  tbody.replaceChildren(frag);
  const count = $('#route-pool-route-count');
  if (count) count.textContent = `${draft.routes.length} ROUTES`;
  renderRoutePoolHud();
}

function renderRoutePoolDashboard() {
  const root = $('#route-pool-dashboard');
  if (!root) return;
  const draft = activeRoutePoolDraft();
  renderRoutePoolSelect();
  renderRoutePoolEditor();
  renderRoutePoolRoutes();
  renderRoutePoolStatus();
  const saveBtn = $('#btn-route-pool-save');
  if (saveBtn) saveBtn.disabled = state.routePoolRunning;
  const restartBtn = $('#btn-route-pool-restart');
  if (restartBtn) restartBtn.disabled = state.routePoolRunning;
}

function selectRoutePoolDraft(poolId) {
  const pool = (state.routePools || []).find((item) => item.id === poolId);
  state.currentRoutePoolId = pool ? pool.id : null;
  state.routePoolDraft = pool ? cloneJson(pool) : newRoutePoolDraft();
  renderRoutePoolDashboard();
}

async function refreshRoutePools({ preserveDraft = false } = {}) {
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.list_route_pools) return;
  const previousDraft = preserveDraft && state.routePoolDraft ? cloneJson(state.routePoolDraft) : null;
  const previousPoolId = preserveDraft ? state.currentRoutePoolId : null;
  try {
    const r = await window.pywebview.api.list_route_pools();
    if (!r || !r.ok) {
      renderRoutePoolStatus(`[ERR] ${(r && r.error) || '连接池读取失败'}`);
      return;
    }
    state.routePools = r.pools || [];
    state.currentRoutePoolId = r.current_id || state.currentRoutePoolId || (state.routePools[0] && state.routePools[0].id) || 'default-pool';
    state.routePoolCandidates = r.candidates || { relay_keys: [], relay_endpoints: [], custom_apis: [] };
    state.routePoolStatus = r.status || null;
    state.routePoolLastStatusAt = Date.now();
    state.routePoolMessage = '';
    if (previousDraft) {
      state.currentRoutePoolId = previousPoolId || state.currentRoutePoolId || 'default-pool';
      state.routePoolDraft = previousDraft;
    } else if (state.currentRoutePoolId) {
      const pool = state.routePools.find((item) => item.id === state.currentRoutePoolId);
      state.routePoolDraft = pool ? cloneJson(pool) : newRoutePoolDraft();
    } else if (!state.routePoolDraft) {
      state.routePoolDraft = newRoutePoolDraft();
    }
    if (state.viewMode === 'pool') renderRoutePoolDashboard();
    const configModal = $('#config-target-modal');
    if (configModal && !configModal.classList.contains('hidden')) renderConfigTargetChoices();
  } catch (err) {
    renderRoutePoolStatus(`[ERR] ${err && err.message ? err.message : String(err)}`);
  }
}

async function refreshRoutePoolRuntimeStatus({ render = true } = {}) {
  if (state.routePoolStatusRefreshInFlight) return;
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.route_pool_status) return;
  state.routePoolStatusRefreshInFlight = true;
  try {
    const r = await window.pywebview.api.route_pool_status();
    state.routePoolStatus = r || null;
    state.routePoolLastStatusAt = Date.now();
    state.routePoolMessage = '';
    if (render && state.viewMode === 'pool') {
      renderRoutePoolRoutes();
      renderRoutePoolStatus();
    }
  } catch (err) {
    state.routePoolStatus = {
      ok: false,
      error: err && err.message ? err.message : String(err),
      logs: state.routePoolStatus && state.routePoolStatus.logs ? state.routePoolStatus.logs : [],
    };
    state.routePoolLastStatusAt = Date.now();
    if (render && state.viewMode === 'pool') {
      renderRoutePoolRoutes();
      renderRoutePoolStatus();
    }
  } finally {
    state.routePoolStatusRefreshInFlight = false;
  }
}

function startRoutePoolStatusRefresh() {
  if (routePoolStatusTimer) return;
  routePoolStatusTimer = setInterval(() => {
    if (state.viewMode === 'pool' && !state.routePoolRunning && !state.dragSorting) {
      refreshRoutePoolRuntimeStatus({ render: true });
    }
  }, 3000);
}

function stopRoutePoolStatusRefresh() {
  if (!routePoolStatusTimer) return;
  clearInterval(routePoolStatusTimer);
  routePoolStatusTimer = null;
}

function createRoutePoolDraft() {
  state.currentRoutePoolId = null;
  state.routePoolDraft = newRoutePoolDraft();
  renderRoutePoolDashboard();
}

function updateRoutePoolHeaderDraft() {
  const draft = activeRoutePoolDraft();
  draft.id = draft.id || state.currentRoutePoolId || 'default-pool';
  draft.name = '连接池';
  draft.model = '';
  renumberRoutePoolRoutes(draft.routes);
}

function addRelayRouteToPool() {
  const candidates = state.routePoolCandidates || {};
  const relaySource = selectedRoutePoolRelaySource();
  const relayDomain = (relaySource && relaySource.domain) || selectValue('#route-pool-relay-domain') || candidates.relay_domain || state.lastDomain || '';
  const keyId = selectValue('#route-pool-relay-key');
  const endpointUrl = selectValue('#route-pool-relay-endpoint');
  const key = ((relaySource && relaySource.keys) || []).find((item) => String(item.id) === String(keyId));
  const endpoint = ((relaySource && relaySource.endpoints) || []).find((item) => item.endpoint === endpointUrl);
  if (!key || !endpoint) {
    setStatus('没有可添加的中转 route', 'warn');
    renderRoutePoolStatus(`[WARN] ${relaySource && relaySource.error ? relaySource.error : '没有可添加的中转 route'}`);
    return false;
  }
  const draft = activeRoutePoolDraft();
  const relayLabel = (relaySource && (relaySource.site || relaySource.domain)) || relayDomain;
  const route = {
    id: routePoolRouteId('relay'),
    label: `${relayLabel} · ${key.name || key.id} · ${key.group_name || '未分组'}`,
    source_type: 'relay',
    relay_domain: relayDomain,
    relay_site: relaySource ? relaySource.site || '' : '',
    key_id: key.id,
    key_name: key.name || String(key.id),
    key_masked: key.key_masked || '',
    group_id: key.group_id,
    group_name: key.group_name || '',
    group_rate: key.group_rate,
    endpoint_name: endpoint.name || '默认',
    base_url: endpoint.endpoint,
    priority: routePoolNextPriority(),
    protocol: 'responses',
    model: state.groupModelColumns[0] || state.models[0] || '',
  };
  draft.routes.push(route);
  state.routePoolEditorOpen = false;
  state.routePoolMessage = '';
  renderRoutePoolDashboard();
  return true;
}

function addSelectedRouteToPool() {
  const sourceType = selectValue('#route-pool-source-type') || 'custom';
  if (sourceType === 'custom') return addCustomRouteToPool();
  return addRelayRouteToPool();
}

function addCustomRouteToPool() {
  const candidates = state.routePoolCandidates || {};
  const apiId = selectValue('#route-pool-custom-api');
  const api = (candidates.custom_apis || []).find((item) => item.id === apiId);
  if (!api) {
    setStatus('没有可添加的自定义 API route', 'warn');
    renderRoutePoolStatus('[WARN] 没有可添加的 API route');
    return false;
  }
  const draft = activeRoutePoolDraft();
  draft.routes.push({
    id: routePoolRouteId('custom'),
    label: customApiName(api),
    source_type: 'custom',
    custom_api_id: api.id,
    custom_api_name: customApiName(api),
    base_url: api.base_url || '',
    priority: routePoolNextPriority(),
    protocol: 'chat',
    model: (api.model_columns && api.model_columns[0]) || draft.model || '',
  });
  state.routePoolEditorOpen = false;
  state.routePoolMessage = '';
  renderRoutePoolDashboard();
  return true;
}

async function saveRoutePool({ silent = false } = {}) {
  updateRoutePoolHeaderDraft();
  const draft = activeRoutePoolDraft();
  draft.id = draft.id || state.currentRoutePoolId || 'default-pool';
  if (!draft.name) draft.name = '连接池';
  state.routePoolRunning = true;
  renderRoutePoolDashboard();
  try {
    const r = await window.pywebview.api.save_route_pool(cloneJson(draft));
    if (!r || !r.ok) {
      if (!silent) setStatus((r && r.error) || '保存连接池失败', 'err');
      return null;
    }
    state.routePools = r.pools || [];
    state.currentRoutePoolId = r.saved_id || r.current_id || state.currentRoutePoolId;
    state.routePoolCandidates = r.candidates || state.routePoolCandidates;
    state.routePoolStatus = r.status || state.routePoolStatus;
    const saved = state.routePools.find((item) => item.id === state.currentRoutePoolId);
    if (saved) state.routePoolDraft = cloneJson(saved);
    if (!silent) setStatus('✓ 已保存连接池', 'ok');
    return state.currentRoutePoolId;
  } catch (err) {
    if (!silent) setStatus(err && err.message ? err.message : String(err), 'err');
    return null;
  } finally {
    state.routePoolRunning = false;
    renderRoutePoolDashboard();
  }
}

async function applyRoutePoolToCodex() {
  if (state.routePoolRunning) return;
  const savedId = await saveRoutePool({ silent: true });
  if (!savedId) return;
  state.routePoolRunning = true;
  renderRoutePoolDashboard();
  renderRoutePoolStatus('[SYS] 正在配置连接池到 Codex...');
  setStatus('配置连接池中…', 'warn');
  try {
    const r = await window.pywebview.api.route_pool_config_apply(savedId);
    const log = buildApplyLog(r || {}, '连接池');
    if (r && r.ok) {
      state.lastInjectBackup = r.backup_name || null;
      setStatus('✓ 连接池已配置到 Codex', 'ok');
      renderRoutePoolStatus(planLog([
        `[READY] 连接池已配置到 Codex`,
        r.backup_name ? `[INFO] backup: ${r.backup_name}` : null,
        r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
      ]));
      await refreshRoutePools();
      return;
    }
    setStatus('连接池配置失败', 'err');
    renderRoutePoolStatus(log);
  } catch (err) {
    setStatus('连接池配置调用失败', 'err');
    renderRoutePoolStatus(`[ERR] ${err && err.message ? err.message : String(err)}`);
  } finally {
    state.routePoolRunning = false;
    renderRoutePoolDashboard();
  }
}

async function restartRoutePoolProxy() {
  if (state.routePoolRunning) return;
  state.routePoolRunning = true;
  renderRoutePoolDashboard();
  renderRoutePoolStatus('[SYS] 正在重启连接池服务...');
  setStatus('重启连接池服务中…', 'warn');
  try {
    const r = await window.pywebview.api.route_pool_restart_proxy();
    const lines = [];
    if (r && r.ok) {
      setStatus('✓ 连接池服务已重启', 'ok');
      lines.push('[READY] 连接池服务已重启');
    } else {
      setStatus('连接池服务重启失败', 'err');
      lines.push(`[ERR] ${(r && r.error) || '连接池服务重启失败'}`);
    }
    if (r && r.stdout) lines.push(`[INFO] stdout:\n${r.stdout}`);
    if (r && r.stderr) lines.push(`[WARN] stderr:\n${r.stderr}`);
    state.routePoolStatus = (r && r.status) || state.routePoolStatus;
    state.routePoolLastStatusAt = Date.now();
    renderRoutePoolStatus(lines.join('\n'));
    await refreshRoutePoolRuntimeStatus({ render: true });
  } catch (err) {
    setStatus('连接池服务重启调用失败', 'err');
    renderRoutePoolStatus(`[ERR] ${err && err.message ? err.message : String(err)}`);
  } finally {
    state.routePoolRunning = false;
    renderRoutePoolDashboard();
  }
}

// ---- custom OpenAI-compatible APIs ----

function customApiName(api) {
  if (!api) return '未命名 API';
  return api.name || api.base_url || api.id || '未命名 API';
}

async function refreshCustomApis() {
  const list = $('#custom-api-list');
  if (!list) return;
  try {
    const r = await window.pywebview.api.list_custom_apis();
    if (!r || !r.ok) {
      list.replaceChildren(el('div', { className: 'muted small', text: (r && r.error) || '(空)' }));
      state.customApis = [];
      state.currentCustomApi = null;
      updateContextLabels();
      return;
    }
    state.customApis = r.apis || [];
    const found = state.customApis.find((a) => a.id === r.current_id);
    if (state.currentCustomApi) {
      const stillThere = state.customApis.find((a) => a.id === state.currentCustomApi.id);
      state.currentCustomApi = stillThere || found || null;
    } else {
      state.currentCustomApi = found || null;
    }
    renderCustomApiList();
    updateContextLabels();
    if (state.viewMode === 'custom') {
      if (state.currentCustomApi) renderCustomApiDashboard();
      else showRelayDashboard();  // the one being viewed got removed elsewhere
    }
  } catch (err) {
    list.replaceChildren(el('div', { className: 'muted small', text: '错: ' + String(err) }));
  }
}

function renderCustomApiList() {
  const list = $('#custom-api-list');
  if (!list) return;
  const frag = document.createDocumentFragment();
  for (const api of state.customApis) {
    const active = state.currentCustomApi && state.currentCustomApi.id === api.id;
    const item = el('div', { className: 'custom-api-item' + (active ? ' active' : '') });
    suppressSidebarContextMenu(item);
    const main = el('div', { className: 'custom-api-main' });
    main.appendChild(el('div', { className: 'custom-api-name', text: customApiName(api) }));
    item.appendChild(main);
    appendSidebarActions(item, [
      sidebarItemButton('删除', {
        className: 'danger',
        title: '删除自定义 API',
        onClick: () => {
          if (confirm(`删除自定义 API ${customApiName(api)}?\n会清掉它在 Keychain 里的 Key (不可恢复).`)) {
            removeCustomApi(api.id);
          }
        },
      }),
    ]);
    item.addEventListener('click', () => selectCustomApi(api.id));
    frag.appendChild(item);
  }
  list.replaceChildren(frag);
}

async function selectCustomApi(id) {
  const local = state.customApis.find((a) => a.id === id);
  if (!local) return;
  state.currentCustomApi = local;
  state.viewMode = 'custom';
  state.customApiResults = {};
  state.customApiColumns = uniqueModels(local.model_columns || []);
  state.customApiModels = uniqueModels(local.model_columns || []);
  state.customApiModelError = null;
  renderMainMode();
  renderCustomApiList();
  updateContextLabels();
  renderCustomApiDashboard();
  try {
    const r = await window.pywebview.api.select_custom_api(id);
    if (r && r.ok) {
      state.customApis = r.apis || state.customApis;
      const next = state.customApis.find((a) => a.id === id);
      if (next) state.currentCustomApi = next;
      renderCustomApiList();
    }
  } catch (_) {}
  loadCustomApiModels(id);
}

async function loadCustomApiModels(id) {
  try {
    const r = await window.pywebview.api.refresh_custom_api_models(id);
    if (!r || !r.ok) return;
    if (!state.currentCustomApi || state.currentCustomApi.id !== id) return;
    state.customApiModels = uniqueModels([...(r.models || []), ...state.customApiColumns]);
    state.customApiModelError = r.model_error || null;
    if ((r.model_columns || []).length && !state.customApiColumns.length) {
      state.customApiColumns = uniqueModels(r.model_columns);
    }
    renderCustomApiDashboard();
  } catch (_) {}
}

function renderCustomApiDashboard() {
  const api = state.currentCustomApi;
  if (!api) return;
  setOptionalText('#custom-name', customApiName(api));
  setOptionalText('#custom-url', api.base_url || '—');
  setOptionalText('#custom-key', api.key_masked || '—');
  const conn = $('#custom-conn');
  if (conn) {
    if (state.customApiModelError) {
      conn.textContent = '读取模型失败';
      conn.className = 'custom-conn err';
    } else {
      conn.textContent = `${state.customApiModels.length} 个模型`;
      conn.className = 'custom-conn ok';
    }
  }
  renderCustomModelTable();
}

function setCustomApiColumns(columns, { persist = true } = {}) {
  // Keep the raw list — blanks are allowed so the user can add an empty row
  // and type a model name not in /models. The backend normalizes/dedupes on
  // persist, and the test/run paths filter out blanks.
  const cols = (Array.isArray(columns) ? columns : []).map((m) => String(m == null ? '' : m));
  state.customApiColumns = cols;
  const active = new Set(cols);
  for (const m of Object.keys(state.customApiResults)) {
    if (!active.has(m)) delete state.customApiResults[m];
  }
  renderCustomModelTable();
  if (persist && state.currentCustomApi) persistCustomApiColumns();
}

async function persistCustomApiColumns() {
  if (!state.currentCustomApi) return;
  try {
    await window.pywebview.api.update_custom_api_columns(state.currentCustomApi.id, state.customApiColumns);
  } catch (_) {}
}

function renderCustomModelTable() {
  const tbody = $('#t-custom-body');
  if (!tbody) return;
  const frag = document.createDocumentFragment();
  state.customApiColumns.forEach((model, idx) => {
    const tr = document.createElement('tr');
    const modelTd = el('td', { className: 'custom-model-cell' });
    const currentModel = String(model || '').trim();
    const options = uniqueModels([currentModel, ...state.customApiModels, ...state.customApiColumns]);
    if (options.length) {
      const select = el('select', {
        className: 'custom-model-select',
        attrs: { 'aria-label': '模型名', title: '模型名' },
      });
      if (!currentModel) {
        select.appendChild(el('option', {
          text: '选择模型',
          attrs: { value: '' },
        }));
      }
      for (const optionModel of options) {
        const opt = el('option', {
          text: optionModel,
          attrs: { value: optionModel },
        });
        if (optionModel === currentModel) opt.selected = true;
        select.appendChild(opt);
      }
      select.addEventListener('change', () => {
        const next = [...state.customApiColumns];
        next[idx] = select.value.trim();
        setCustomApiColumns(next);
      });
      modelTd.appendChild(select);
    } else {
      const input = el('input', {
        className: 'custom-model-input',
        attrs: {
          type: 'text', value: model,
          placeholder: '模型名', 'aria-label': '模型名', spellcheck: 'false',
        },
      });
      input.addEventListener('change', () => {
        const next = [...state.customApiColumns];
        next[idx] = input.value.trim();
        setCustomApiColumns(next);
      });
      modelTd.appendChild(input);
    }
    tr.appendChild(modelTd);

    const resTd = el('td', { className: 'right custom-model-result' });
    resTd.appendChild(buildTag(state.customApiResults[model]));
    tr.appendChild(resTd);

    const actTd = el('td', { className: 'right custom-model-actions' });
    actTd.appendChild(el('button', {
      className: 'link',
      text: '测试',
      attrs: { type: 'button', title: '测试此模型' },
      onClick: () => testCustomApiModels([model]),
    }));
    if (state.customApiColumns.length > 1) {
      actTd.appendChild(el('button', {
        className: 'group-model-remove',
        text: '×',
        attrs: { type: 'button', title: '移除', 'aria-label': '移除模型' },
        onClick: () => setCustomApiColumns(state.customApiColumns.filter((_, i) => i !== idx)),
      }));
    }
    tr.appendChild(actTd);
    frag.appendChild(tr);
  });
  if (!state.customApiColumns.length) {
    const tr = document.createElement('tr');
    tr.appendChild(el('td', {
      className: 'muted small',
      text: '还没有要测试的模型，点「+ 模型」添加',
      attrs: { colspan: '3' },
    }));
    frag.appendChild(tr);
  }
  tbody.replaceChildren(frag);
  updateCustomTestButton();
}

function updateCustomTestButton() {
  const btn = $('#btn-custom-test-all');
  if (!btn) return;
  const testable = state.customApiColumns.filter((m) => String(m || '').trim()).length;
  btn.disabled = testable === 0;
  btn.textContent = testable > 0 ? `测试 ${testable}` : '测试';
}

function addCustomModelRow() {
  const existing = new Set(state.customApiColumns.map((m) => String(m || '').trim()));
  const candidate = uniqueModels(state.customApiModels).find((m) => !existing.has(m));
  setCustomApiColumns([...state.customApiColumns, candidate || '']);
  requestAnimationFrame(() => {
    const inputs = document.querySelectorAll('#t-custom-body .custom-model-input, #t-custom-body .custom-model-select');
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
  });
}

async function testCustomApiModels(models) {
  const api = state.currentCustomApi;
  if (!api) return;
  const targets = uniqueModels(models).filter((m) => String(m || '').trim());
  if (!targets.length) return;
  for (const m of targets) state.customApiResults[m] = { running: true };
  renderCustomModelTable();
  let done = 0;
  for (const model of targets) {
    setStatus(`测试模型 ${done + 1}/${targets.length}…`, 'warn');
    try {
      const r = await window.pywebview.api.test_custom_api_model(api.id, model);
      state.customApiResults[model] = (r && r.ok && r.result)
        ? r.result
        : { ok: false, status: 'err', summary: (r && r.error) || '调用失败' };
    } catch (err) {
      state.customApiResults[model] = { ok: false, status: 'err', summary: String(err) };
    }
    done++;
    renderCustomModelTable();
  }
  setStatus(`✓ 完成 ${done}/${targets.length} 个模型`, 'ok');
}

async function removeCustomApi(id) {
  try {
    const r = await window.pywebview.api.remove_custom_api(id);
    if (!r || !r.ok) {
      setStatus((r && r.error) || '删除失败', 'err');
      return;
    }
    if (state.currentCustomApi && state.currentCustomApi.id === id) {
      state.currentCustomApi = null;
      if (state.viewMode === 'custom') showRelayDashboard();
    }
    state.customApis = r.apis || [];
    renderCustomApiList();
    updateContextLabels();
    setStatus('✓ 已删除自定义 API', 'ok');
  } catch (err) {
    setStatus(String(err), 'err');
  }
}

// ---- add custom API modal ----

function openAddCustomApiModal() {
  $('#add-custom-url').value = '';
  $('#add-custom-key').value = '';
  $('#add-custom-name').value = '';
  resetAddCustomApiUI();
  $('#add-custom-api-modal').classList.remove('hidden');
  setTimeout(() => $('#add-custom-url').focus(), 50);
}

function closeAddCustomApiModal() {
  $('#add-custom-api-modal').classList.add('hidden');
}

function resetAddCustomApiUI() {
  const result = $('#add-custom-probe-result');
  result.className = 'probe-result hidden';
  result.textContent = '';
  const errBox = $('#add-custom-error');
  errBox.classList.add('hidden');
  errBox.textContent = '';
  const btn = $('#btn-add-custom-submit');
  btn.disabled = false;
  btn.textContent = '创建';
}

async function submitAddCustomApi() {
  const url = $('#add-custom-url').value.trim();
  const key = $('#add-custom-key').value.trim();
  const name = $('#add-custom-name').value.trim();
  const errBox = $('#add-custom-error');
  const result = $('#add-custom-probe-result');
  errBox.classList.add('hidden');
  errBox.textContent = '';
  if (!url || !key) {
    errBox.textContent = 'URL 和 API Key 必填';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#btn-add-custom-submit');
  btn.disabled = true;
  btn.textContent = '测试连通中…';
  result.className = 'probe-result probe-info';
  result.textContent = '正在请求 /v1/models 测连通…';
  result.classList.remove('hidden');
  try {
    const r = await window.pywebview.api.add_custom_api(url, key, name);
    if (!r || !r.ok) {
      result.classList.add('hidden');
      errBox.textContent = (r && r.error) || '创建失败';
      errBox.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = '创建';
      return;
    }
    state.customApis = r.apis || [];
    state.customApiModels = uniqueModels(r.models || []);
    closeAddCustomApiModal();
    if (r.added_id) {
      await selectCustomApi(r.added_id);
    } else {
      refreshCustomApis();
    }
    setStatus(`✓ 已添加自定义 API · ${(r.models || []).length} 个模型`, 'ok');
  } catch (err) {
    result.classList.add('hidden');
    errBox.textContent = err && err.message ? err.message : String(err);
    errBox.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '创建';
  }
}

async function removeRelay(domain) {
  showScreen('loading');
  try {
    const data = await window.pywebview.api.remove_relay(domain);
    refreshSidebar();
    if (!data.ok && !data.needs_setup) {
      showError('删除失败', data.error || '未知错误');
      return;
    }
    if (data.needs_setup) {
      showError('请添加中转', '已删除最后一个中转, 点中转标题右侧 添加');
      return;
    }
    // when current relay was removed, backend already re-bootstrapped to next relay
    if (data.switched_to) {
      applyBootstrap(data);
      showScreen('dashboard');
    }
  } catch (err) {
    showError('删除失败', err && err.message ? err.message : String(err));
  }
}

async function switchRelay(domain) {
  setStatus(`切到 ${domain}…`, 'warn');
  showScreen('loading');
  try {
    const data = (await window.pywebview.api.switch_relay(domain)) || {};
    if (!data.ok) {
      if (data.needs_login) {
        showLoginError(data.domain || domain, `请先在浏览器登录 ${data.domain || domain}`);
      } else {
        state.retryRelayDomain = null;
        showError('切 relay 失败', data.error);
      }
      setStatus('未就绪', 'err');
      return;
    }
    state.pingResults = {};
    state.groupResults = {};
    state.viewMode = 'relay';
    state.retryRelayDomain = null;
    applyBootstrap(data);
    renderMainMode();
    showScreen('dashboard');
    setStatus('✓ 已切 relay', 'ok');
  } catch (err) {
    showError('切 relay 失败', err && err.message ? err.message : String(err));
    setStatus('错误', 'err');
  }
}

// ---- account dropdown (Keychain-backed) ----

function openAccountPop() {
  $('#account-pop').classList.remove('hidden');
  loadAccountList();
}

function closeAccountPop() {
  $('#account-pop').classList.add('hidden');
}

function updateAccountDeleteButton(email) {
  const btn = $('#btn-account-delete');
  if (!btn) return;
  btn.disabled = !email;
  btn.dataset.email = email || '';
  btn.textContent = '删除';
}

function fmtTimestamp(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadAccountList() {
  const list = $('#account-list');
  list.replaceChildren(el('div', { className: 'muted small', text: '加载中…' }));
  try {
    const r = (await window.pywebview.api.list_accounts()) || {};
    if (!r.ok) {
      list.replaceChildren(el('div', { className: 'muted small', text: r.error || '(空)' }));
      updateAccountDeleteButton(null);
      return;
    }
    updateAccountChip(r.current || null);
    if (!r.accounts.length) {
      list.replaceChildren(el('div', {
        className: 'muted small',
        text: '尚未保存账号; 点下面"+ 新增账号"',
      }));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const acc of r.accounts) {
      const isCur = acc.email === r.current;
      const row = el('div', { className: 'account-row' + (isCur ? ' current' : '') });
      const main = el('div', { className: 'account-row-main' });
      main.appendChild(el('div', { className: 'account-email', text: acc.email }));
      main.appendChild(el('div', {
        className: 'account-row-meta',
        text: (isCur ? '当前 · ' : '') + '上次校验 ' + fmtTimestamp(acc.last_verified),
      }));
      row.appendChild(main);
      if (!isCur) {
        main.style.cursor = 'pointer';
        main.addEventListener('click', () => switchAccount(acc.email));
      }
      frag.appendChild(row);
    }
    list.replaceChildren(frag);
  } catch (err) {
    list.replaceChildren(el('div', { className: 'muted small', text: '错: ' + String(err) }));
  }
}

// ---- add-account modal ----

function openAddAccountModal() {
  $('#add-account-email').value = '';
  $('#add-account-password').value = '';
  $('#add-account-error').classList.add('hidden');
  $('#add-account-error').textContent = '';
  const submit = $('#btn-add-account-submit');
  submit.disabled = false;
  submit.textContent = '添加';
  const hint = $('#add-account-relay-hint');
  if (hint) hint.textContent = state.lastDomain || '当前 relay';
  closeAccountPop();
  $('#add-account-modal').classList.remove('hidden');
  setTimeout(() => $('#add-account-email').focus(), 50);
}

function closeAddAccountModal() {
  $('#add-account-modal').classList.add('hidden');
}

async function submitAddAccount() {
  const email = $('#add-account-email').value.trim();
  const password = $('#add-account-password').value;
  const errBox = $('#add-account-error');
  errBox.classList.add('hidden');
  errBox.textContent = '';
  if (!email || !password) {
    errBox.textContent = 'email 和密码都必填';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#btn-add-account-submit');
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    const r = await window.pywebview.api.add_account(email, password);
    if (!r || !r.ok) {
      errBox.textContent = (r && r.error) || '添加失败';
      errBox.classList.remove('hidden');
      return;
    }
    closeAddAccountModal();
    applyBootstrap(r);
    setStatus(`✓ ${email}`, 'ok');
    showScreen('dashboard');
  } catch (err) {
    errBox.textContent = err && err.message ? err.message : String(err);
    errBox.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '添加';
  }
}

function openAddCodexAccountModal() {
  const title = $('#add-codex-title');
  if (title) title.textContent = '➕ 新增 Codex 官方账号';
  $('#add-codex-slot').value = '';
  $('#add-codex-display').value = '';
  $('#add-codex-error').classList.add('hidden');
  $('#add-codex-error').textContent = '';
  const submit = $('#btn-add-codex-submit');
  submit.disabled = false;
  submit.textContent = '登录并添加';
  $('#add-codex-account-modal').classList.remove('hidden');
  setTimeout(() => $('#add-codex-slot').focus(), 50);
}

function openReloginCodexAccountModal(acc = state.currentCodexAccount) {
  openAddCodexAccountModal();
  if (!acc) return;
  const title = $('#add-codex-title');
  if (title) title.textContent = '↻ 重新登录 Codex 官方账号';
  $('#add-codex-slot').value = acc.slot || '';
  $('#add-codex-display').value = acc.display_name || '';
  $('#add-codex-error').textContent = '该账号的本地 OAuth refresh token 已失效。重新登录会覆盖同名 slot 的登录文件。';
  $('#add-codex-error').classList.remove('hidden');
}

function closeAddCodexAccountModal() {
  $('#add-codex-account-modal').classList.add('hidden');
}

async function submitAddCodexAccount() {
  const slot = $('#add-codex-slot').value.trim();
  const display = $('#add-codex-display').value.trim();
  const errBox = $('#add-codex-error');
  errBox.classList.add('hidden');
  errBox.textContent = '';
  if (!slot) {
    errBox.textContent = '账号标识必填';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#btn-add-codex-submit');
  btn.disabled = true;
  btn.textContent = '等待登录…';
  setStatus('正在打开 Codex 登录…', 'warn');
  try {
    const r = await window.pywebview.api.add_codex_account(slot, display);
    if (!r || !r.ok) {
      errBox.textContent = (r && (r.error || r.stderr || r.stdout)) || '添加失败';
      errBox.classList.remove('hidden');
      return;
    }
    closeAddCodexAccountModal();
    state.codexAccounts = r.accounts || [];
    state.currentCodexAccount = (r.accounts || []).find((a) => a.slot === slot) || r.current_official || state.currentCodexAccount;
    state.viewMode = 'official';
    renderMainMode();
    renderCodexAccountList();
    updateContextLabels();
    renderOfficialDashboard(state.currentCodexAccount);
    setStatus('✓ 已登录并添加官方账号', 'ok');
  } catch (err) {
    errBox.textContent = err && err.message ? err.message : String(err);
    errBox.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '登录并添加';
  }
}

async function removeCodexAccount(acc = state.currentCodexAccount) {
  if (!acc || !acc.slot) {
    showError('删除官方账号失败', '请先选择官方账号');
    return;
  }
  const label = acc.email || acc.display_name || acc.slot;
  if (!confirm(`删除官方账号 ${label}?\n会移除 sub2cli 保存的本地登录副本；不会删除 Codex 历史。`)) return;
  setStatus(`删除官方账号 ${label}…`, 'warn');
  try {
    const r = await window.pywebview.api.remove_codex_account(acc.slot);
    if (!r || !r.ok) {
      showError('删除官方账号失败', (r && r.error) || '删除失败');
      return;
    }
    state.codexAccounts = applyStoredOrder(r.accounts || [], CODEX_ORDER_KEY, codexAccountKey);
    state.currentCodexAccount = r.current_official || state.codexAccounts[0] || null;
    renderCodexAccountList();
    updateContextLabels();
    if (state.viewMode === 'official') renderOfficialDashboard(state.currentCodexAccount);
    setStatus(`✓ 已删除官方账号 ${label}`, 'ok');
  } catch (err) {
    showError('删除官方账号失败', err && err.message ? err.message : String(err));
  }
}

async function switchAccount(email) {
  setStatus(`切到 ${email}…`, 'warn');
  closeAccountPop();
  showScreen('loading');
  try {
    const data = (await window.pywebview.api.switch_account(email)) || {};
    if (!data.ok) {
      showError('切账号失败', data.error);
      setStatus('错误', 'err');
      return;
    }
    state.pingResults = {};
    state.groupResults = {};
    applyBootstrap(data);
    showScreen('dashboard');
    setStatus(`✓ 已切 ${email}`, 'ok');
  } catch (err) {
    showError('切账号失败', err && err.message ? err.message : String(err));
    setStatus('错误', 'err');
  }
}

async function deleteAccount(email) {
  if (!email) return;
  try {
    const r = (await window.pywebview.api.delete_account(email)) || {};
    if (!r.ok) {
      setStatus(r.error || '删除失败', 'err');
      return;
    }
    if (state.currentEmail === email) updateAccountChip(null);
    setStatus(`✓ 删除 ${email}`, 'ok');
    await loadAccountList();
  } catch (err) {
    setStatus('错误', 'err');
  }
}

function deleteCurrentAccount() {
  const btn = $('#btn-account-delete');
  const email = (btn && btn.dataset.email) || state.currentEmail;
  deleteAccount(email);
}

// ---- configure Codex modal (choice → direct apply + rollback) ----

function selectValue(id) {
  const node = $(id);
  return node ? String(node.value || '').trim() : '';
}

function setSelectOptions(select, rows, selectedValue, emptyText) {
  if (!select) return;
  select.replaceChildren();
  if (!rows.length) {
    select.appendChild(el('option', {
      text: emptyText,
      attrs: { value: '', disabled: 'true', selected: 'true' },
    }));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const row of rows) {
    const option = el('option', {
      text: row.label,
      attrs: { value: row.value },
    });
    select.appendChild(option);
  }
  const values = new Set(rows.map((row) => row.value));
  select.value = values.has(selectedValue) ? selectedValue : rows[0].value;
}

function renderConfigTargetChoices() {
  const relayRows = (state.relays || []).map((relay) => ({
    value: relay.domain,
    label: relay.site || relay.domain,
  }));
  const customRows = (state.customApis || []).map((api) => ({
    value: api.id,
    label: customApiName(api),
  }));
  const officialRows = (state.codexAccounts || []).map((acc) => ({
    value: acc.slot,
    label: acc.email || acc.display_name || acc.slot,
  }));
  const poolLabel = $('#choice-pool-label');
  if (poolLabel) poolLabel.textContent = '连接池';
  setSelectOptions($('#choice-relay'), relayRows, state.lastDomain || '', '未设置');
  setSelectOptions($('#choice-custom'), customRows, state.currentCustomApi ? state.currentCustomApi.id : '', '未设置');
  setSelectOptions($('#choice-codex'), officialRows, state.currentCodexAccount ? state.currentCodexAccount.slot : '', '未设置');
  updateConfigChoiceMeta();
}

function updateConfigChoiceMeta() {
  const pool = (state.routePools || []).find((item) => item.id === state.currentRoutePoolId)
    || state.routePools[0]
    || null;
  const poolMeta = $('#choice-pool-meta');
  if (poolMeta) {
    poolMeta.textContent = pool
      ? `${pool.routes ? pool.routes.length : 0} routes · fail=${(pool.policy && pool.policy.fail_consecutive) || 2}`
      : '请先点顶部「管理连接池」添加 route 并保存';
  }

  const relayValue = selectValue('#choice-relay');
  const relay = (state.relays || []).find((item) => item.domain === relayValue);
  const relayMeta = $('#choice-relay-meta');
  if (relayMeta) {
    if (relay) {
      const currentRelaySelected = relay.domain === state.lastDomain;
      const currentKeyName = state.codexKey && state.codexKey.name;
      const currentEndpointName = state.defaultEndpoint && state.defaultEndpoint.name;
      const keyName = currentRelaySelected && currentKeyName
        ? currentKeyName
        : (relay.codex_key_name || relay.default_key_name || '未选 key');
      const endpointName = currentRelaySelected && currentEndpointName
        ? currentEndpointName
        : (relay.default_endpoint_name || '默认端点');
      relayMeta.textContent = `Codex key: ${keyName} · 端点: ${endpointName}`;
    } else {
      relayMeta.textContent = '请先在左侧「中转」新增或选择';
    }
  }

  const customValue = selectValue('#choice-custom');
  const custom = (state.customApis || []).find((api) => api.id === customValue);
  const customMeta = $('#choice-custom-meta');
  if (customMeta) {
    customMeta.textContent = custom
      ? custom.base_url || '使用保存好的自定义 url + key'
      : '请先在左侧「自定义api」新增或选择';
  }

  const officialValue = selectValue('#choice-codex');
  const official = (state.codexAccounts || []).find((acc) => acc.slot === officialValue);
  const officialMeta = $('#choice-codex-meta');
  if (officialMeta) {
    officialMeta.textContent = official
      ? `${official.plan_label || 'Codex'} · ${official.slot}${official.is_registered ? '' : ' · 待注册'}`
      : '请先在官号列表新增或选择账号';
  }

  const buttons = configTargetButtons();
  if (!state.configTargetRunning) {
    if (buttons.pool) buttons.pool.disabled = !pool || !(pool.routes || []).length;
    if (buttons.relay) buttons.relay.disabled = !relay;
    if (buttons.custom) buttons.custom.disabled = !custom;
    if (buttons.official) buttons.official.disabled = !official;
  }
}

function openConfigTargetModal() {
  resetConfigTargetUi();
  renderConfigTargetChoices();
  refreshRoutePools();
  $('#config-target-modal').classList.remove('hidden');
}

function closeConfigTargetModal() {
  if (state.configTargetRunning) return;
  $('#config-target-modal').classList.add('hidden');
}

async function startRelayConfig() {
  state.currentInjectTarget = 'relay';
  const domain = selectValue('#choice-relay');
  if (!domain) {
    renderConfigTargetResult('warn', '没有可配置的中转', '请先在左侧中转列表新增或选择一个中转。', { tag: '[WARN]' });
    setStatus('请先选择中转', 'warn');
    return;
  }
  if (domain !== state.lastDomain) {
    if (state.configTargetRunning) return;
    state.configTargetRunning = true;
    setConfigTargetBusy('relay', '切换中…');
    renderConfigTargetResult('running', '正在切换中转', `先切换到 ${domain}, 再配置到 Codex。`);
    try {
      const data = await window.pywebview.api.switch_relay(domain);
      if (!data || !data.ok) {
        renderConfigTargetResult(
          data && data.needs_login ? 'warn' : 'err',
          data && data.needs_login ? '中转需要重新登录' : '切换中转失败',
          (data && (data.error || `请先在浏览器登录 ${data.domain || domain}`)) || '切换失败。',
          { tag: data && data.needs_login ? '[WARN]' : '[ERR]' }
        );
        setStatus('切换中转失败', 'err');
        return;
      }
      state.pingResults = {};
      state.groupResults = {};
      state.viewMode = 'relay';
      state.retryRelayDomain = null;
      applyBootstrap(data);
      renderMainMode();
      showScreen('dashboard');
      renderConfigTargetChoices();
    } catch (err) {
      renderConfigTargetResult('err', '切换中转失败', err && err.message ? err.message : String(err), { tag: '[ERR]' });
      setStatus('切换中转失败', 'err');
      return;
    } finally {
      state.configTargetRunning = false;
      resetConfigTargetButtons();
    }
  }
  if (window.pywebview && window.pywebview.api && window.pywebview.api.relay_config_status) {
    try {
      const r = await window.pywebview.api.relay_config_status();
      if (r && r.ok && r.already_current) {
        renderConfigTargetResult(
          'ok',
          '已经是该渠道',
          `${r.label || configTargetDetail('relay')}。未重新配置。`,
          { tag: '[READY]' }
        );
        setStatus('✓ 已经是该渠道', 'ok');
        return;
      }
    } catch (_err) {
      // Fall back to the normal configure path; inject_apply still has rollback.
    }
  }
  applyConfigTarget('relay');
}

function startPoolConfig() {
  state.currentInjectTarget = 'pool';
  const pool = (state.routePools || []).find((item) => item.id === state.currentRoutePoolId)
    || state.routePools[0]
    || null;
  const id = pool ? pool.id : '';
  if (!id) {
    renderConfigTargetResult('warn', '没有可配置的连接池', '请先点顶部「管理连接池」添加 route 并保存。', { tag: '[WARN]' });
    setStatus('请先保存连接池 route', 'warn');
    return;
  }
  state.currentRoutePoolId = id;
  if (pool) state.routePoolDraft = cloneJson(pool);
  applyConfigTarget('pool');
}

function startOfficialConfig() {
  state.currentInjectTarget = 'official';
  const slot = selectValue('#choice-codex');
  if (!slot) {
    renderConfigTargetResult('warn', '没有可配置的官方账号', '请先在官号列表新增或选择一个官方账号。', { tag: '[WARN]' });
    setStatus('请先选择官方账号', 'warn');
    return;
  }
  const local = (state.codexAccounts || []).find((acc) => acc.slot === slot);
  if (local) state.currentCodexAccount = local;
  if (!state.currentCodexAccount || state.currentCodexAccount.slot !== slot) {
    renderConfigTargetResult('warn', '官方账号不存在', `未找到官方账号槽位 ${slot}。`, { tag: '[WARN]' });
    return;
  }
  window.pywebview.api.select_codex_account(slot)
    .then((r) => {
      if (r && r.ok) {
        state.codexAccounts = applyStoredOrder(r.accounts || state.codexAccounts, CODEX_ORDER_KEY, codexAccountKey);
        state.currentCodexAccount = r.current_official || state.currentCodexAccount;
        renderCodexAccountList();
        if (state.viewMode === 'official') renderOfficialDashboard(state.currentCodexAccount);
        renderConfigTargetChoices();
      }
      applyConfigTarget('official');
    })
    .catch(() => applyConfigTarget('official'));
}

function startCustomConfig() {
  state.currentInjectTarget = 'custom';
  const id = selectValue('#choice-custom');
  if (!id) {
    renderConfigTargetResult('warn', '没有可配置的自定义 API', '请先在左侧「自定义api」新增或选择一个 API。', { tag: '[WARN]' });
    setStatus('请先选择自定义 API', 'warn');
    return;
  }
  const local = (state.customApis || []).find((api) => api.id === id);
  if (local) state.currentCustomApi = local;
  window.pywebview.api.select_custom_api(id)
    .then((r) => {
      if (r && r.ok) {
        state.customApis = r.apis || state.customApis;
        state.currentCustomApi = (state.customApis || []).find((api) => api.id === id) || state.currentCustomApi;
        renderCustomApiList();
        renderConfigTargetChoices();
      }
      applyConfigTarget('custom');
    })
    .catch(() => applyConfigTarget('custom'));
}

function resetConfigTargetUi() {
  state.lastInjectBackup = null;
  state.configTargetRunning = false;
  const result = $('#config-target-result');
  if (result) {
    result.className = 'config-result hidden';
    result.replaceChildren();
  }
  resetConfigTargetButtons();
}

function configTargetButtons() {
  return {
    pool: $('#btn-config-pool'),
    relay: $('#btn-config-relay'),
    custom: $('#btn-config-custom'),
    official: $('#btn-config-official'),
  };
}

function resetConfigTargetButtons() {
  Object.values(configTargetButtons()).forEach((btn) => {
    if (!btn) return;
    btn.disabled = false;
    btn.textContent = '配置';
  });
  updateConfigChoiceMeta();
}

function setConfigTargetBusy(target, text = '配置中…') {
  const btns = configTargetButtons();
  const active = btns[target] || btns.relay;
  Object.values(btns).forEach((btn) => {
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = btn === active ? text : '等待';
  });
}

function configTargetTitle(target) {
  if (target === 'pool') return '连接池';
  if (target === 'official') return '官方账号';
  if (target === 'custom') return '自定义 API';
  return '中转';
}

function configTargetDetail(target) {
  if (target === 'pool') {
    const pool = (state.routePools || []).find((item) => item.id === state.currentRoutePoolId);
    return pool ? `${pool.name || pool.id} · ${(pool.routes || []).length} routes` : '未选择连接池';
  }
  if (target === 'official') {
    const acc = state.currentCodexAccount;
    return acc ? `${acc.email || acc.display_name || acc.slot} · ${acc.plan_label || 'Codex'}` : '未选择官方账号';
  }
  if (target === 'custom') {
    const api = state.currentCustomApi;
    return api ? `${customApiName(api)} · ${api.base_url || ''}` : '未选择自定义 API';
  }
  const key = state.codexKey && state.codexKey.name ? state.codexKey.name : '未选 key';
  const ep = state.defaultEndpoint && state.defaultEndpoint.name ? state.defaultEndpoint.name : '默认端点';
  return `${currentRelayLabel()} · Codex key ${key} · ${ep}`;
}

function buildApplyLog(r, targetLabel) {
  const autoRollback = r && r.auto_rollback;
  return planLog([
    r && r.ok ? `[READY] ${targetLabel} configured.` : `[ERR] ${targetLabel} configure failed.`,
    r && r.error ? `[ERR] reason: ${r.error}` : null,
    r && r.returncode !== undefined ? `[INFO] returncode: ${r.returncode ?? 'timeout'}` : null,
    r && r.backup_name ? `[INFO] backup: ${r.backup_name}` : null,
    r && r.rollback_command ? `[INFO] undo command: ${r.rollback_command}` : null,
    r && r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
    r && r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
    autoRollback ? `[INFO] auto rollback: ${autoRollback.ok ? 'ok' : 'failed'}` : null,
    autoRollback && autoRollback.error ? `[ERR] rollback reason: ${autoRollback.error}` : null,
    autoRollback && autoRollback.returncode !== undefined ? `[INFO] rollback returncode: ${autoRollback.returncode ?? 'timeout'}` : null,
    autoRollback && autoRollback.stdout ? `[INFO] rollback stdout:\n${autoRollback.stdout}` : null,
    autoRollback && autoRollback.stderr ? `[INFO] rollback stderr:\n${autoRollback.stderr}` : null,
  ]);
}

function renderConfigTargetResult(kind, title, detail, options = {}) {
  const box = $('#config-target-result');
  if (!box) return;
  box.className = `config-result ${kind || 'running'}`;
  const tagText = options.tag || (kind === 'ok' ? '[READY]' : kind === 'err' ? '[ERR]' : kind === 'warn' ? '[WARN]' : '[SYS]');
  const children = [
    el('div', {
      className: 'config-result-head',
      children: [
        el('div', { className: 'config-result-title', text: title }),
        el('div', { className: 'config-result-tag', text: tagText }),
      ],
    }),
    el('div', { className: 'config-result-detail', text: detail || '' }),
  ];
  if (options.log) {
    children.push(el('pre', { className: 'config-result-log', text: options.log }));
  }
  const actionButtons = [];
  if (options.relogin) {
    actionButtons.push(el('button', {
      className: 'btn-action primary',
      text: '重新登录此账号',
      attrs: { type: 'button' },
      onClick: () => openReloginCodexAccountModal(options.reloginAccount || state.currentCodexAccount),
    }));
  }
  if (options.undo && state.lastInjectBackup) {
    actionButtons.push(el('button', {
      className: `btn-action ${options.undoDanger === false ? '' : 'danger'}`,
      text: options.undoLabel || '撤销本次配置',
      attrs: { type: 'button' },
      onClick: undoConfigTarget,
    }));
  }
  if (actionButtons.length) {
    children.push(el('div', {
      className: 'config-result-actions',
      children: actionButtons,
    }));
  }
  box.replaceChildren(...children);
}

async function applyConfigTarget(target) {
  if (state.configTargetRunning) return;
  const isOfficial = target === 'official';
  const isCustom = target === 'custom';
  const isPool = target === 'pool';
  if (isPool && !state.currentRoutePoolId) {
    renderConfigTargetResult('warn', '没有可配置的连接池', '请先选择一个已保存的连接池。', { tag: '[WARN]' });
    setStatus('请先选择连接池', 'warn');
    return;
  }
  if (isOfficial && !state.currentCodexAccount) {
    renderConfigTargetResult('warn', '没有可配置的官方账号', '请先在左侧官号列表新增或选择一个官方账号。', { tag: '[WARN]' });
    setStatus('请先选择官方账号', 'warn');
    return;
  }
  if (isCustom && !state.currentCustomApi) {
    renderConfigTargetResult('warn', '没有可配置的自定义 API', '请先在左侧「自定义api」新增或选择一个 API。', { tag: '[WARN]' });
    setStatus('请先选择自定义 API', 'warn');
    return;
  }

  state.configTargetRunning = true;
  state.currentInjectTarget = target;
  state.lastInjectBackup = null;
  setConfigTargetBusy(target);
  setStatus('配置中…', 'warn');
  renderConfigTargetResult(
    'running',
    `正在配置 ${configTargetTitle(target)} 到 Codex`,
    `${configTargetDetail(target)}。正在写入 auth.json / config.toml, 成功后会显示撤销按钮。`
  );

  try {
    let r;
    if (isOfficial) {
      r = await window.pywebview.api.codex_account_config_apply(state.currentCodexAccount && state.currentCodexAccount.slot);
    } else if (isPool) {
      r = await window.pywebview.api.route_pool_config_apply(state.currentRoutePoolId);
    } else if (isCustom) {
      r = await window.pywebview.api.custom_api_config_apply(state.currentCustomApi && state.currentCustomApi.id);
    } else {
      r = await window.pywebview.api.inject_apply();
    }
    const autoRollback = r && r.auto_rollback;
    const log = buildApplyLog(r || {}, configTargetTitle(target));

    if (r && r.ok) {
      state.lastInjectBackup = r.backup_name || null;
      renderConfigTargetResult(
        'ok',
        '配置成功',
        state.lastInjectBackup
          ? `Codex 已切到${configTargetTitle(target)}。保留撤销点 ${state.lastInjectBackup}, 需要时可一键恢复到配置前。`
          : `Codex 已切到${configTargetTitle(target)}。本次输出未识别到撤销点。`,
        { log, undo: !!state.lastInjectBackup }
      );
      setStatus('✓ 配置完成', 'ok');
      if (isOfficial) refreshCodexAccounts();
      if (isPool) refreshRoutePools();
      return;
    }

    const rollbackOk = !!(autoRollback && autoRollback.ok);
    const rollbackFailed = !!(r && r.backup_name && (!autoRollback || !autoRollback.ok));
    const needsRelogin = isOfficial && /refresh token|登录态已失效|signing in again|重新登录/i.test(String((r && r.error) || '') + '\n' + log);
    state.lastInjectBackup = rollbackFailed ? r.backup_name : null;
    renderConfigTargetResult(
      rollbackOk ? 'warn' : 'err',
      rollbackOk ? '配置失败，已自动回滚' : '配置失败',
      rollbackOk
        ? `写入失败后已恢复到配置前状态。下面日志可直接反馈给开发者排查。`
        : (rollbackFailed
          ? `写入失败，自动回滚未确认成功。保留 ${r.backup_name}, 可点击重试回滚并把日志反馈给开发者。`
          : (needsRelogin
            ? `该官方账号的本地登录刷新链已失效，需要重新登录一次。未写入 Codex 配置。`
            : `配置失败发生在生成撤销点之前，未识别到可回滚备份。下面日志可直接反馈给开发者。`)),
      {
        tag: rollbackOk ? '[ROLLBACK]' : '[ERR]',
        log,
        undo: rollbackFailed,
        undoLabel: '重试回滚',
        relogin: needsRelogin,
        reloginAccount: state.currentCodexAccount,
      }
    );
    setStatus('✗ 配置失败', 'err');
  } catch (err) {
    state.lastInjectBackup = null;
    renderConfigTargetResult(
      'err',
      '配置调用失败',
      err && err.message ? err.message : String(err),
      { log: planLog([`[ERR] exception: ${err && err.stack ? err.stack : String(err)}`]) }
    );
    setStatus('配置失败', 'err');
  } finally {
    state.configTargetRunning = false;
    resetConfigTargetButtons();
  }
}

async function undoConfigTarget() {
  const backupName = state.lastInjectBackup;
  if (!backupName || state.configTargetRunning) return;
  state.configTargetRunning = true;
  setConfigTargetBusy(state.currentInjectTarget, '回滚中…');
  setStatus('撤销配置中…', 'warn');
  renderConfigTargetResult('running', '正在撤销本次配置', `回滚到 ${backupName}, 恢复配置前的 Codex auth/config/profile 状态。`);
  try {
    const r = await window.pywebview.api.inject_rollback(backupName);
    renderConfigTargetResult(
      r.ok ? 'ok' : 'err',
      r.ok ? '已撤销本次配置' : '撤销失败',
      r.ok ? `已恢复 ${backupName}。` : `rollback 返回 rc=${r.returncode ?? '?'}, 请把日志反馈给开发者。`,
      {
        tag: r.ok ? '[READY]' : '[ERR]',
        log: planLog([
          r.error ? `[ERR] reason: ${r.error}` : null,
          r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
          r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
        ]),
        undo: !r.ok,
        undoLabel: '重试回滚',
      }
    );
    setStatus(r.ok ? '✓ 已撤销配置' : '✗ 撤销失败', r.ok ? 'ok' : 'err');
    if (r.ok) state.lastInjectBackup = null;
  } catch (err) {
    renderConfigTargetResult(
      'err',
      '撤销调用失败',
      err && err.message ? err.message : String(err),
      { log: planLog([`[ERR] exception: ${err && err.stack ? err.stack : String(err)}`]), undo: true, undoLabel: '重试回滚' }
    );
    setStatus('撤销失败', 'err');
  } finally {
    state.configTargetRunning = false;
    resetConfigTargetButtons();
  }
}

function openInjectModal() {
  $('#inject-modal').classList.remove('hidden');
  state.lastInjectBackup = null;
  state.lastInjectChanges = null;
  const undoBtn = $('#btn-inject-undo');
  undoBtn.classList.add('hidden');
  undoBtn.disabled = true;
  const confirmBtn = $('#btn-inject-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '执行中…';
  confirmBtn.classList.add('hidden');
  requestAnimationFrame(() => {
    applyInject();
  });
}

function closeInjectModal() {
  $('#inject-modal').classList.add('hidden');
}

function updateInjectScrollbar() {
  const body = $('#inject-body');
  const bar = $('#inject-scrollbar');
  const thumb = $('#inject-scroll-thumb');
  if (!body || !bar || !thumb) return;

  const scrollable = body.scrollHeight > body.clientHeight + 1;
  bar.classList.toggle('visible', scrollable);
  if (!scrollable) {
    body.scrollTop = 0;
    thumb.style.height = '';
    thumb.style.transform = 'translateY(0)';
    return;
  }

  const trackHeight = bar.clientHeight;
  const thumbHeight = Math.max(36, Math.round((body.clientHeight / body.scrollHeight) * trackHeight));
  const maxTop = Math.max(0, trackHeight - thumbHeight);
  const maxScroll = Math.max(1, body.scrollHeight - body.clientHeight);
  const top = Math.round((body.scrollTop / maxScroll) * maxTop);
  thumb.style.height = `${thumbHeight}px`;
  thumb.style.transform = `translateY(${top}px)`;
}

function scrollInjectBody(deltaY) {
  const body = $('#inject-body');
  if (!body) return false;
  const maxScroll = Math.max(0, body.scrollHeight - body.clientHeight);
  if (!maxScroll) return false;
  const next = Math.max(0, Math.min(maxScroll, body.scrollTop + deltaY));
  if (next === body.scrollTop) return false;
  body.scrollTop = next;
  updateInjectScrollbar();
  return true;
}

function planLog(lines) {
  return lines.filter((line) => line !== null && line !== undefined && line !== '').join('\n');
}

function buildInjectStep(step) {
  return el('div', {
    className: `inject-step ${step.state || 'pending'}`,
    children: [
      el('div', { className: 'inject-step-dot', text: step.icon || '' }),
      el('div', {
        className: 'inject-step-body',
        children: [
          el('div', { className: 'inject-step-title', text: step.title }),
          el('div', { className: 'inject-step-detail', text: step.detail || '' }),
        ],
      }),
    ],
  });
}

function buildInjectChange(change) {
  const children = [
    el('div', {
      className: 'inject-change-head',
      children: [
        el('div', { className: 'inject-change-path', text: change.path || change.title || '' }),
        el('div', { className: 'inject-change-detail', text: change.detail || '' }),
      ],
    }),
  ];

  if (change.rows && change.rows.length) {
    children.push(el('div', {
      className: 'inject-diff-rows',
      children: change.rows.map((row) => el('div', {
        className: `inject-diff-row ${row.before === row.after ? 'same' : 'changed'}`,
        children: [
          el('div', { className: 'inject-diff-label', text: row.label || '' }),
          el('div', { className: 'inject-diff-value before', text: row.before ?? '' }),
          el('div', { className: 'inject-diff-arrow', text: '→' }),
          el('div', { className: 'inject-diff-value after', text: row.after ?? '' }),
        ],
      })),
    }));
  }

  if (change.diff && change.diff.length) {
    children.push(el('pre', {
      className: 'inject-diff-code',
      children: change.diff.map((line) => el('span', {
        className: `inject-diff-line ${line.kind || 'context'}`,
        text: line.text || '',
      })),
    }));
  }

  return el('div', { className: 'inject-change', children });
}

function renderInjectState(title, steps, options = {}) {
  const body = $('#inject-body');
  const children = [
    el('div', { className: 'inject-headline', text: title }),
    el('div', { className: 'inject-steps', children: steps.map(buildInjectStep) }),
  ];
  if (options.changes && options.changes.length) {
    children.push(el('div', { className: 'inject-section-label', text: '将修改' }));
    children.push(el('div', {
      className: 'inject-change-list',
      children: options.changes.map(buildInjectChange),
    }));
  }
  if (options.log) {
    children.push(el('div', { className: `plan-label ${options.logClass || ''}`, text: options.logTitle || '[INFO] 详细输出' }));
    children.push(el('pre', { className: 'plan-preview inject-log', text: options.log }));
  }
  body.replaceChildren(...children);
  requestAnimationFrame(updateInjectScrollbar);
}

async function applyInject() {
  // Share the in-flight flag with applyConfigTarget so the two mutating inject
  // flows can't interleave and clobber state.lastInjectBackup (which would point
  // the undo button at the wrong backup).
  if (state.configTargetRunning) return;
  state.configTargetRunning = true;
  const confirmBtn = $('#btn-inject-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '执行中…';
  confirmBtn.classList.add('hidden');
  $('#btn-inject-undo').classList.add('hidden');
  $('#btn-inject-undo').disabled = true;
  setStatus('配置中…', 'warn');
  const isOfficial = state.currentInjectTarget === 'official';
  renderInjectState('正在配置 Codex', [
    { state: 'ok', title: `读取${isOfficial ? '官方账号' : '当前中转'}`, detail: isOfficial ? '使用已选择的官方账号作为配置目标' : '使用 API Keys 中选用的 Codex key 和端点作为配置目标' },
    { state: 'running', title: '写入 Codex 配置', detail: isOfficial ? '导入账号 auth、切换 auth.json、更新 config.toml 为 OAuth 模式' : '保存 API 渠道、切换 auth.json、更新 config.toml' },
    { state: 'pending', title: '刷新 Codex App', detail: '写入成功后会按 sub2cli-inject 逻辑重启/打开 Codex' },
    { state: 'pending', title: '准备撤销点', detail: '成功后会显示撤销本次配置按钮' },
  ], {
    changes: state.lastInjectChanges || (isOfficial ? [
      { path: '~/.codex/provider-slots.json', detail: '切换 current 到官方账号槽位' },
      { path: '~/.codex/auth.json', detail: '复制官方账号 OAuth 登录文件' },
      { path: '~/.codex/config.toml', detail: '切回 Codex OAuth provider' },
      { path: 'Codex App', detail: '需要时重新打开以加载官方账号' },
    ] : [
      { path: '~/.codex/provider-slots.json', detail: '新增或更新当前中转的 API 渠道' },
      { path: '~/.codex/auth.json', detail: '切换到该渠道的 API key 登录文件' },
      { path: '~/.codex/config.toml', detail: '写入 Codex provider 的 base_url/model 配置' },
      { path: '~/Library/Application Support/Codex', detail: '保持或切换 Codex App profile symlink' },
      { path: 'Codex 历史 session', detail: '保留现有历史, 不批量改写旧会话 provider' },
      { path: 'Codex App', detail: '需要时重新打开以加载新配置' },
    ]),
  });
  try {
    const r = isOfficial
      ? await window.pywebview.api.codex_account_config_apply(state.currentCodexAccount && state.currentCodexAccount.slot)
      : await window.pywebview.api.inject_apply();
    state.lastInjectBackup = r.backup_name || null;
    const autoRollback = r.auto_rollback || null;
    renderInjectState(r.ok ? '配置完成' : '配置失败', [
      { state: 'ok', title: `读取${isOfficial ? '官方账号' : '当前中转'}`, detail: '已完成' },
      {
        state: r.ok ? 'ok' : 'err',
        title: r.ok ? '写入 Codex 配置完成' : '写入 Codex 配置失败',
        detail: r.ok ? 'sub2cli-inject 返回 rc=0' : `sub2cli-inject 返回 rc=${r.returncode ?? '?'}`,
      },
      {
        state: r.ok ? 'ok' : 'pending',
        title: '刷新 Codex App',
        detail: r.ok ? '已按配置器输出处理' : '未完成',
      },
      {
        state: r.ok && r.backup_name ? 'ok' : (r.ok ? 'warn' : (autoRollback ? (autoRollback.ok ? 'ok' : 'err') : 'pending')),
        title: r.ok ? '撤销点' : '失败回滚',
        detail: r.ok
          ? (r.backup_name ? `可回滚到 ${r.backup_name}` : '未从输出中识别到回滚备份名')
          : (autoRollback ? (autoRollback.ok ? `已自动回滚 ${autoRollback.backup_name}` : `自动回滚失败: ${autoRollback.error || ('rc=' + (autoRollback.returncode ?? '?'))}`) : '未生成可回滚备份'),
      },
    ], {
      logClass: r.ok ? 'plan-result ok' : 'plan-result err',
      logTitle: r.ok ? '[READY] 配置输出' : '[ERR] 配置输出',
      log: buildApplyLog(r, isOfficial ? '官方账号' : '当前中转'),
    });
    if (r.ok && r.backup_name) {
      const undoBtn = $('#btn-inject-undo');
      undoBtn.classList.remove('hidden');
      undoBtn.disabled = false;
    }
    confirmBtn.textContent = r.ok ? '[READY] 完成' : '[ERR] 失败';
    setStatus(r.ok ? '✓ 配置完成' : '✗ 配置失败', r.ok ? 'ok' : 'err');
    if (r.ok && isOfficial) refreshCodexAccounts();
  } catch (err) {
    setStatus('错误', 'err');
    confirmBtn.textContent = '[ERR] 失败';
    renderInjectState('配置调用失败', [
      { state: 'err', title: '调用失败', detail: err && err.message ? err.message : String(err) },
      { state: 'pending', title: '撤销点', detail: '未完成, 不提供撤销' },
    ]);
  } finally {
    state.configTargetRunning = false;
  }
}

async function undoInject() {
  const backupName = state.lastInjectBackup;
  if (!backupName || state.configTargetRunning) return;
  state.configTargetRunning = true;
  const undoBtn = $('#btn-inject-undo');
  undoBtn.disabled = true;
  $('#btn-inject-confirm').disabled = true;
  setStatus('撤销配置中…', 'warn');
  renderInjectState('正在撤销本次配置', [
    { state: 'running', title: '恢复备份', detail: `回滚到 ${backupName}` },
    { state: 'pending', title: '恢复 Codex 配置', detail: '恢复 auth.json/config.toml/App profile 等状态' },
  ]);
  try {
    const r = await window.pywebview.api.inject_rollback(backupName);
    renderInjectState(r.ok ? '已撤销本次配置' : '撤销失败', [
      { state: r.ok ? 'ok' : 'err', title: '恢复备份', detail: r.ok ? `已恢复 ${backupName}` : `rollback 返回 rc=${r.returncode ?? '?'}` },
      { state: r.ok ? 'ok' : 'pending', title: 'Codex 配置', detail: r.ok ? '已恢复到配置前状态' : '未确认恢复' },
    ], {
      logClass: r.ok ? 'plan-result ok' : 'plan-result err',
      logTitle: r.ok ? '[READY] 撤销输出' : '[ERR] 撤销输出',
      log: planLog([
        r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
        r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
      ]),
    });
    setStatus(r.ok ? '✓ 已撤销配置' : '✗ 撤销失败', r.ok ? 'ok' : 'err');
    if (r.ok) {
      state.lastInjectBackup = null;
      undoBtn.classList.add('hidden');
    } else {
      undoBtn.disabled = false;
    }
  } catch (err) {
    setStatus('撤销失败', 'err');
    undoBtn.disabled = false;
    renderInjectState('撤销调用失败', [
      { state: 'err', title: '调用失败', detail: err && err.message ? err.message : String(err) },
    ]);
  } finally {
    state.configTargetRunning = false;
  }
}

// ---- renderers ----

function usageWindow(snapshot, name) {
  const limits = snapshot && snapshot.rate_limits && snapshot.rate_limits.rateLimits;
  if (!limits) return null;
  return limits[name] || null;
}

function setOptionalText(selector, text) {
  const node = $(selector);
  if (node) node.textContent = text;
}

function setUsageWindow(prefix, win, secondaryText) {
  const used = win && typeof win.usedPercent === 'number' ? win.usedPercent : null;
  const left = used == null ? '—' : `${Math.max(0, Math.round(100 - used))}%`;
  const usedWidth = used == null ? 0 : Math.max(0, Math.min(100, used));
  $(`#${prefix}-used-bar`).style.width = `${usedWidth}%`;
  $(`#${prefix}-left`).textContent = left;
  $(`#${prefix}-reset`).textContent = win ? fmtDurationFromNow(win.resetsAt) : '—';
  if (prefix === 'session') {
    $('#session-reserve').textContent = secondaryText || '—';
  } else {
    $('#weekly-deficit').textContent = secondaryText || '—';
  }
}

async function renderOfficialDashboard(acc) {
  acc = acc || state.currentCodexAccount;
  if (!acc) {
    $('#official-email').textContent = '未添加官方账号';
    $('#official-plan').textContent = '—';
    $('#official-updated').textContent = 'Updated —';
    setOptionalText('#official-slot', '—');
    setOptionalText('#official-auth', '—');
    setOptionalText('#official-profile', '—');
    setUsageWindow('session', null, '—');
    setUsageWindow('weekly', null, '—');
    $('#official-usage-source').textContent = '[INFO] 点官号列表标题右侧 添加 创建 Codex 官方账号槽位';
    return;
  }
  $('#official-email').textContent = acc.email || acc.display_name || acc.slot;
  $('#official-plan').textContent = codexPlanTitle(acc);
  $('#official-updated').textContent = 'Updating…';
  setOptionalText('#official-slot', acc.slot || '—');
  setOptionalText('#official-auth', acc.source_auth_file && acc.source_auth_file !== acc.auth_file
    ? `${acc.source_auth_file} → ${acc.auth_file || '—'}`
    : (acc.auth_file || '—'));
  setOptionalText('#official-profile', acc.app_profile_dir || '—');
  $('#official-usage-source').textContent = '[INFO] 正在读取 Codex app-server 用量…';
  setUsageWindow('session', null, '—');
  setUsageWindow('weekly', null, '—');
  try {
    const r = await window.pywebview.api.codex_account_usage(acc.slot);
    if (!r.ok) {
      $('#official-usage-source').textContent = `[ERR] ${r.error || '读取失败'}`;
      return;
    }
    const account = r.account || acc;
    if (account) {
      state.currentCodexAccount = account;
      $('#official-email').textContent = account.email || account.display_name || account.slot;
      $('#official-plan').textContent = codexPlanTitle(account);
      setOptionalText('#official-auth', account.source_auth_file && account.source_auth_file !== account.auth_file
        ? `${account.source_auth_file} → ${account.auth_file || '—'}`
        : (account.auth_file || '—'));
      setOptionalText('#official-profile', account.app_profile_dir || '—');
      updateContextLabels();
    }
    const snapshot = r.snapshot;
    if (snapshot && snapshot.error) {
      setUsageWindow('session', null, '—');
      setUsageWindow('weekly', null, '—');
      $('#official-updated').textContent = fmtAgo(account.last_refresh);
      $('#official-usage-source').textContent = `[WARN] ${snapshot.error}`;
      return;
    }
    const rpcAccount = snapshot && snapshot.account && snapshot.account.account;
    if (rpcAccount) {
      $('#official-email').textContent = rpcAccount.email || $('#official-email').textContent;
      $('#official-plan').textContent = rpcAccount.planType ? codexPlanTitle(account) : $('#official-plan').textContent;
    }
    const primary = usageWindow(snapshot, 'primary');
    const secondary = usageWindow(snapshot, 'secondary');
    setUsageWindow('session', primary, primary ? `${Math.max(0, Math.round(primary.usedPercent || 0))}%` : '—');
    setUsageWindow('weekly', secondary, secondary ? `${Math.max(0, Math.round(secondary.usedPercent || 0))}%` : '—');
    $('#official-updated').textContent = snapshot
      ? fmtAgo(new Date().toISOString())
      : fmtAgo(account.last_refresh);
    $('#official-usage-source').textContent = snapshot
      ? '[READY] Codex app-server · account/read + account/rateLimits/read'
      : '[WARN] 未能读取实时用量, 仅显示本地 auth 信息';
  } catch (err) {
    $('#official-updated').textContent = fmtAgo(acc.last_refresh);
    $('#official-usage-source').textContent = `[ERR] ${err && err.message ? err.message : String(err)}`;
  }
}

function renderAccount(data) {
  const u = data.user || {};
  $('#acc-email').textContent = u.email || '—';
  const status = u.status || '—';
  // grid row 状态: "Active (正常)" or "状态值"
  const statusRow = $('#acc-status');
  if (statusRow) {
    statusRow.textContent = status === 'active' ? 'Active (正常)' : status;
    statusRow.className = 'grid-value' + (status === 'active' ? ' highlight-green' : '');
  }
  $('#acc-balance').textContent = fmtMoney(u.balance);
  $('#acc-concurrency').textContent = u.concurrency != null ? String(u.concurrency) : '—';
}

function subscriptionIsActive(sub) {
  if (!sub || sub.status !== 'active') return false;
  if (!sub.expires_at) return true;
  const expiresMs = Date.parse(sub.expires_at);
  return !Number.isFinite(expiresMs) || expiresMs > Date.now();
}

function buildSubscriptionUsage(label, used, limit) {
  const usedNum = asNumber(used);
  const limitNum = asNumber(limit);
  const pct = usedNum == null || !limitNum || limitNum <= 0
    ? 0
    : Math.max(0, Math.min(100, (usedNum / limitNum) * 100));
  const row = el('div', { className: 'subscription-usage-row' });
  row.appendChild(el('span', { className: 'subscription-usage-label', text: label }));
  const bar = el('span', { className: 'subscription-usage-bar', attrs: { 'aria-hidden': 'true' } });
  bar.appendChild(el('span', { attrs: { style: `width: ${pct.toFixed(1)}%;` } }));
  row.appendChild(bar);
  row.appendChild(el('span', {
    className: 'subscription-usage-value',
    text: `${fmtUsd(usedNum)}/${fmtUsd(limitNum)}`,
  }));
  return row;
}

function buildSubscriptionItem(sub) {
  const item = el('div', { className: 'subscription-item' });
  const head = el('div', { className: 'subscription-head' });
  const rateLabel = sub.rate_multiplier != null ? ` (${sub.rate_multiplier}x)` : '';
  head.appendChild(el('div', { className: 'subscription-name', text: `${sub.group_name || '未命名卡'}${rateLabel}` }));
  head.appendChild(el('div', { className: 'subscription-remain', text: fmtRemainingDays(sub.expires_at) }));
  item.appendChild(head);

  const usage = el('div', { className: 'subscription-usage' });
  usage.appendChild(buildSubscriptionUsage('每日', sub.daily_usage_usd, sub.daily_limit_usd));
  usage.appendChild(buildSubscriptionUsage('每周', sub.weekly_usage_usd, sub.weekly_limit_usd));
  usage.appendChild(buildSubscriptionUsage('每月', sub.monthly_usage_usd, sub.monthly_limit_usd));
  item.appendChild(usage);
  return item;
}

function renderSubscriptions(subscriptions) {
  const list = $('#subscription-list');
  const count = $('#subscription-count');
  if (!list || !count) return;
  const all = Array.isArray(subscriptions) ? subscriptions : [];
  const active = all.filter(subscriptionIsActive);
  count.textContent = `${active.length} ACTIVE`;
  count.classList.toggle('empty', active.length === 0);

  const frag = document.createDocumentFragment();
  if (!all.length) {
    frag.appendChild(el('div', { className: 'subscription-empty', text: '[INFO] 未读取到订阅卡' }));
  } else if (!active.length) {
    frag.appendChild(el('div', { className: 'subscription-empty warn', text: '[WARN] 无有效订阅卡' }));
  } else {
    for (const sub of active) frag.appendChild(buildSubscriptionItem(sub));
  }
  list.replaceChildren(frag);
}

function buildGroupSelect(currentGroupId, options = {}) {
  const select = el('select', {
    className: options.className || 'key-group-select',
    attrs: {
      title: options.title || '选择分组',
      'aria-label': options.ariaLabel || '选择分组',
      disabled: options.disabled ? 'true' : null,
    },
  });
  for (const group of state.groups) {
    const option = el('option', {
      text: `${group.name || '?'} (${fmtRate(group.rate_multiplier)})`,
      attrs: { value: group.id },
    });
    if (String(group.id) === String(currentGroupId)) option.selected = true;
    select.appendChild(option);
  }
  return select;
}

function buildKeyActions(k, isCodexKey) {
  const actions = el('div', { className: 'key-actions' });
  actions.appendChild(el('button', {
    className: 'link sm key-action-btn',
    text: 'key',
    attrs: { type: 'button', title: `显示 ${k.name || 'key'} 的完整 key` },
    onClick: () => openKeySecret(k),
  }));
  actions.appendChild(el('button', {
    className: 'link sm key-action-btn' + (isCodexKey ? ' active' : ''),
    text: isCodexKey ? '已选' : '选用',
    attrs: {
      type: 'button',
      title: isCodexKey ? '配置 Codex 时已使用此 key' : '配置 Codex 时使用此 key',
      disabled: isCodexKey ? 'true' : null,
    },
    onClick: () => selectCodexKey(k.id),
  }));
  return actions;
}

function buildKeyRow(k, defaultKey, codexKey) {
  const tr = document.createElement('tr');
  const isTestKey = !!(k.is_test_key || (defaultKey && String(defaultKey.id) === String(k.id)));
  const isCodexKey = !!(k.is_codex_key || (codexKey && String(codexKey.id) === String(k.id)));
  if (isTestKey) tr.classList.add('test-key');
  if (isCodexKey) tr.classList.add('codex-key');
  tr.appendChild(el('td', {
    children: [
      el('span', { className: 'key-row-name', text: k.name || '?' }),
      isTestKey ? el('span', { className: 'tag dim key-row-badge', text: '测试' }) : null,
      isCodexKey ? el('span', { className: 'tag running key-row-badge', text: 'Codex' }) : null,
    ],
  }));
  tr.appendChild(el('td', { className: 'mono', text: k.key_masked || '—' }));
  const groupTd = el('td');
  if (isTestKey) {
    groupTd.appendChild(el('span', { className: 'key-test-auto', text: '测试时自动切换' }));
  } else {
    const select = buildGroupSelect(k.group_id, {
      title: `选择 ${k.name || 'key'} 的分组`,
      ariaLabel: `选择 ${k.name || 'key'} 的分组`,
    });
    select.addEventListener('change', () => updateKeyGroup(k.id, select.value));
    groupTd.appendChild(select);
  }
  tr.appendChild(groupTd);
  tr.appendChild(el('td', {
    className: 'right',
    children: [buildKeyActions(k, isCodexKey)],
  }));
  return tr;
}

function renderKeys(keys, _groups, defaultKey, codexKey = state.codexKey) {
  const tbody = $('#t-keys-body');
  if (!tbody) return;
  const frag = document.createDocumentFragment();
  if (!keys.length) {
    frag.appendChild(el('tr', {
      children: [el('td', { text: '暂无 key', attrs: { colspan: '4' } })],
    }));
  } else {
    for (const k of keys) frag.appendChild(buildKeyRow(k, defaultKey, codexKey));
  }
  tbody.replaceChildren(frag);
}

function buildEndpointRow(ep, isCurrent, pingResult) {
  const tr = document.createElement('tr');
  if (isCurrent) tr.classList.add('current');

  tr.appendChild(el('td', { text: ep.name || '?' }));
  tr.appendChild(el('td', { className: 'mono', text: ep.endpoint || '' }));

  const latencyTd = el('td', { className: 'right' });
  latencyTd.appendChild(buildTag(pingResult));
  tr.appendChild(latencyTd);

  const actionsTd = el('td', { className: 'right' });
  if (isCurrent) {
    actionsTd.appendChild(el('span', { className: 'switch-action inactive', text: '当前选用' }));
  } else {
    actionsTd.appendChild(el('button', {
      className: 'link sm',
      text: '选用',
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

function buildGroupHeader() {
  const thead = $('#t-groups-head');
  if (!thead) return;
  const tr = document.createElement('tr');
  const total = state.groups.length;
  const selected = getSelectedGroupIds().length;
  const allSelected = total > 0 && selected === total;
  const checkboxTh = el('th', { className: 'checkbox-col', attrs: { 'aria-label': '选择' } });
  const selectAll = el('button', {
    className: 'group-select-mark group-select-all' + (allSelected ? ' active' : ''),
    attrs: {
      type: 'button',
      title: allSelected ? '清空已选分组' : '全选分组用于批量测速',
      'aria-label': allSelected ? '清空已选分组' : '全选分组用于批量测速',
      'aria-pressed': allSelected ? 'true' : 'false',
      disabled: total === 0 ? 'true' : null,
    },
    onClick: toggleAllGroups,
  });
  checkboxTh.appendChild(selectAll);
  tr.appendChild(checkboxTh);
  tr.appendChild(el('th', { text: '倍率' }));
  tr.appendChild(el('th', { text: '名称' }));

  const hasUnusedModel = hasUnusedGroupModelCandidate();
  state.groupModelColumns.forEach((model, idx) => {
    const th = el('th', { className: 'right group-model-col' });
    const wrap = el('div', { className: 'group-model-head' });
    const select = el('select', {
      className: 'group-model-select',
      attrs: { title: model || '选择模型', 'aria-label': '模型' },
    });
    for (const optModel of modelOptionsForColumn(model, idx)) {
      const option = el('option', { text: optModel, attrs: { value: optModel } });
      if (optModel === model) option.selected = true;
      select.appendChild(option);
    }
    select.value = model;
    select.addEventListener('change', () => {
      const next = [...state.groupModelColumns];
      next[idx] = select.value;
      setGroupModelColumns(next);
    });
    wrap.appendChild(select);
    if (state.groupModelColumns.length > 1) {
      wrap.appendChild(el('button', {
        className: 'group-model-remove',
        text: '×',
        attrs: { type: 'button', title: `移除模型${idx + 1}`, 'aria-label': `移除模型${idx + 1}` },
        onClick: () => {
          const next = state.groupModelColumns.filter((_, i) => i !== idx);
          setGroupModelColumns(next);
        },
      }));
    }
    if (idx === state.groupModelColumns.length - 1) {
      const addBtn = el('button', {
        className: 'group-model-add',
        text: '+',
        attrs: {
          type: 'button',
          title: hasUnusedModel ? '添加模型列' : '没有更多可添加的模型',
          'aria-label': hasUnusedModel ? '添加模型列' : '没有更多可添加的模型',
          disabled: !hasUnusedModel ? 'true' : null,
        },
        onClick: addGroupModelColumn,
      });
      wrap.appendChild(addBtn);
    }
    th.appendChild(wrap);
    tr.appendChild(th);
  });

  thead.replaceChildren(tr);
}

function hasUnusedGroupModelCandidate() {
  const existing = new Set(state.groupModelColumns);
  const groupOptionLists = Object.values(state.groupModels || {});
  const candidates = uniqueModels([...state.models, ...groupOptionLists.flat()]);
  return candidates.some((model) => !existing.has(model));
}

function addGroupModelColumn() {
  const existing = new Set(state.groupModelColumns);
  const groupOptionLists = Object.values(state.groupModels || {});
  const candidates = uniqueModels([...state.models, ...groupOptionLists.flat()]);
  if (!candidates.length) {
    setStatus(state.modelError || '模型列表为空', 'warn');
    return;
  }
  const nextModel = candidates.find((m) => !existing.has(m));
  if (!nextModel) {
    setStatus('所有已读取模型都在表格中', 'warn');
    return;
  }
  setGroupModelColumns([...state.groupModelColumns, nextModel]);
}

function buildGroupRow(g, isDefaultHere, groupResult) {
  const tr = document.createElement('tr');
  const selectedForTest = !!state.groupSelected[g.id];
  if (selectedForTest) tr.classList.add('selected-for-test');

  const checkboxTd = el('td', { className: 'checkbox-col group-select-cell' });
  const selectBtn = el('button', {
    className: 'group-select-mark' + (selectedForTest ? ' active' : ''),
    attrs: {
      type: 'button',
      'aria-pressed': selectedForTest ? 'true' : 'false',
      'aria-label': selectedForTest ? '取消选择此分组' : '选择此分组用于批量测速',
      title: selectedForTest ? '取消选择此分组' : '选择此分组用于批量测速',
    },
    onClick: () => {
      const next = !state.groupSelected[g.id];
      state.groupSelected[g.id] = next;
      selectBtn.classList.toggle('active', next);
      selectBtn.setAttribute('aria-pressed', next ? 'true' : 'false');
      selectBtn.setAttribute('aria-label', next ? '取消选择此分组' : '选择此分组用于批量测速');
      selectBtn.title = next ? '取消选择此分组' : '选择此分组用于批量测速';
      tr.classList.toggle('selected-for-test', next);
      updateBatchTestButton();
      buildGroupHeader();
    },
  });
  checkboxTd.appendChild(selectBtn);
  checkboxTd.addEventListener('click', (e) => {
    if (e.target === selectBtn) return;
    selectBtn.click();
  });
  tr.appendChild(checkboxTd);

  tr.appendChild(el('td', { text: fmtRate(g.rate_multiplier) }));
  tr.appendChild(el('td', { text: g.name || '?' }));

  for (const model of state.groupModelColumns) {
    const modelTd = el('td', { className: 'right group-model-cell' });
    modelTd.appendChild(buildTag(groupResult && groupResult[model]));
    tr.appendChild(modelTd);
  }

  return tr;
}

function renderGroups(groups, _keys, defaultKey) {
  buildGroupHeader();
  const tbody = $('#t-groups-body');
  const sorted = [...groups].sort((a, b) => (a.rate_multiplier ?? 99999) - (b.rate_multiplier ?? 99999));
  const frag = document.createDocumentFragment();
  for (const g of sorted) {
    const isDefaultHere = defaultKey && defaultKey.group_id === g.id;
    const r = state.groupResults[g.id];
    frag.appendChild(buildGroupRow(g, isDefaultHere, r));
  }
  tbody.replaceChildren(frag);
  updateBatchTestButton();
}

// ---- batch group test (selection rail + serial probe) ----

function getSelectedGroupIds() {
  return state.groups.filter((g) => state.groupSelected[g.id]).map((g) => g.id);
}

function updateBatchTestButton() {
  const btn = $('#btn-test-selected-groups');
  const n = getSelectedGroupIds().length;
  const hasModels = state.groupModelColumns.length > 0 && state.groupModelColumns.every(Boolean);
  if (btn) {
    btn.disabled = state.groupTestRunning || n === 0 || !hasModels;
    btn.textContent = state.groupTestRunning
      ? '测试中…'
      : (n > 0 ? `测试 ${n}` : '测试');
  }
}

function toggleAllGroups() {
  const total = state.groups.length;
  const sel = getSelectedGroupIds().length;
  if (total > 0 && sel === total) {
    state.groupSelected = {};
  } else {
    for (const g of state.groups) state.groupSelected[g.id] = true;
  }
  renderGroups(state.groups, [], state.defaultKey);
}

async function testSelectedGroups() {
  if (state.groupTestRunning) return;
  const ids = getSelectedGroupIds();
  const columns = uniqueModels(state.groupModelColumns);
  if (!ids.length || !columns.length) return;
  state.groupTestRunning = true;
  // SERIAL: server-side "current default group" is per-account state;
  // parallel calls would all race on the switch and end up hitting whichever
  // group landed last. Must do switch → send → await → next, one at a time.
  try {
    for (const id of ids) {
      state.groupResults[id] = Object.fromEntries(columns.map((model) => [model, { running: true }]));
    }
    renderGroups(state.groups, [], state.defaultKey);
    let done = 0;
    for (const id of ids) {
      setStatus(`串行测试 ${done + 1}/${ids.length}…`, 'warn');
      try {
        const r = await window.pywebview.api.test_group(id, columns);
        if (!r.ok) {
          state.groupResults[id] = Object.fromEntries(
            columns.map((model) => [model, { ok: false, status: 'err', summary: r.error }])
          );
        } else {
          state.groupResults[id] = r.results || {};
          if (r.restore_error) {
            for (const model of columns) {
              if (state.groupResults[id][model]) {
                state.groupResults[id][model].summary = `${state.groupResults[id][model].summary || ''} · 恢复分组失败: ${r.restore_error}`;
              }
            }
          }
          if (r.default_key) state.defaultKey = r.default_key;
          if (r.codex_key) state.codexKey = r.codex_key;
          if (r.keys) state.keys = r.keys;
        }
      } catch (err) {
        state.groupResults[id] = Object.fromEntries(
          columns.map((model) => [model, { ok: false, status: 'err', summary: String(err) }])
        );
      }
      done++;
      renderGroups(state.groups, [], state.defaultKey);  // 每个完成立刻刷
      renderKeys(state.keys, state.groups, state.defaultKey, state.codexKey);
    }
    setStatus(`✓ 完成 ${done}/${ids.length} 个分组`, 'ok');
  } finally {
    state.groupTestRunning = false;
    renderGroups(state.groups, [], state.defaultKey);
  }
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
    if (state.defaultEndpoint) {
      syncCurrentRelaySummary({
        default_endpoint_name: state.defaultEndpoint.name,
      });
    }
    renderEndpoints(state.endpoints, state.defaultEndpoint);
    updateContextLabels();
    setStatus('✓ 已切端点', 'ok');
  } catch (err) {
    setStatus('错误', 'err');
  }
}

async function selectCodexKey(keyId) {
  setStatus('切 Codex key 中…', 'warn');
  try {
    const r = await window.pywebview.api.set_codex_key(keyId);
    if (!r.ok) {
      setStatus(r.error || '切 Codex key 失败', 'err');
      return;
    }
    state.codexKey = r.codex_key || null;
    if (r.keys) state.keys = r.keys;
    if (state.codexKey) {
      syncCurrentRelaySummary({
        codex_key_name: state.codexKey.name,
        default_key_name: state.codexKey.name,
      });
    }
    renderKeys(state.keys, state.groups, state.defaultKey, state.codexKey);
    updateContextLabels();
    setStatus('✓ 已切 Codex key', 'ok');
  } catch (err) {
    setStatus('错误', 'err');
  }
}

async function updateKeyGroup(keyId, groupId) {
  setStatus('切 key 分组中…', 'warn');
  try {
    const r = await window.pywebview.api.update_key_group(keyId, groupId);
    if (!r.ok) {
      setStatus(r.error || '切分组失败', 'err');
    } else {
      state.keys = r.keys || state.keys.map((k) => String(k.id) === String(r.key.id) ? r.key : k);
      if (r.codex_key) state.codexKey = r.codex_key;
      if (state.defaultKey && String(state.defaultKey.id) === String(r.key.id)) state.defaultKey = r.key;
      renderKeys(state.keys, state.groups, state.defaultKey, state.codexKey);
      renderGroups(state.groups, [], state.defaultKey);
      updateContextLabels();
      setStatus('✓ 已更新 key 分组', 'ok');
    }
  } catch (err) {
    setStatus('错误', 'err');
  }
}

async function openKeySecret(k) {
  if (!k || k.id === undefined || k.id === null) return;
  const modal = $('#key-secret-modal');
  const nameEl = $('#key-secret-name');
  const valueEl = $('#key-secret-value');
  state.keySecret = null;
  nameEl.textContent = k.name || 'API Key';
  valueEl.textContent = '读取中...';
  modal.classList.remove('hidden');
  try {
    const r = await window.pywebview.api.reveal_key(k.id);
    if (!r.ok) {
      valueEl.textContent = r.error || '读取失败';
      setStatus(r.error || '读取 key 失败', 'err');
      return;
    }
    state.keySecret = r.key || '';
    nameEl.textContent = r.name || k.name || 'API Key';
    valueEl.textContent = state.keySecret || '—';
  } catch (err) {
    valueEl.textContent = err && err.message ? err.message : String(err);
    setStatus('错误', 'err');
  }
}

function closeKeySecret() {
  $('#key-secret-modal').classList.add('hidden');
  state.keySecret = null;
  $('#key-secret-value').textContent = '—';
}

async function copyKeySecret() {
  const text = state.keySecret || $('#key-secret-value').textContent || '';
  if (!text || text === '—' || text === '读取中...') return;
  try {
    await navigator.clipboard.writeText(text);
    setStatus('✓ key 已复制', 'ok');
  } catch (_err) {
    setStatus('复制失败', 'err');
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
    const hintLabel = check.severity === 'warn' ? '提示: ' : '修复: ';
    body.appendChild(el('div', { className: 'check-hint', text: hintLabel + check.fix_hint }));
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
    const r = (await window.pywebview.api.check_health()) || {};
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

let bootstrapStarted = false;

function startBootstrapOnce() {
  if (bootstrapStarted) return;
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.bootstrap) return;
  bootstrapStarted = true;
  setStatus('桥就绪, bootstrap…', 'warn');
  // macOS: kill native white titlebar, blend with our dark header
  if (window.pywebview && window.pywebview.api && window.pywebview.api.customize_chrome) {
    window.pywebview.api.customize_chrome().catch(() => {});
  }
  installHeaderDrag();
  bootstrap().finally(() => {
    initVersionChip();
  });
}

window.addEventListener('pywebviewready', startBootstrapOnce);

window.addEventListener('DOMContentLoaded', () => {
  startBootstrapOnce();
  let tries = 0;
  const timer = setInterval(() => {
    if (bootstrapStarted) {
      clearInterval(timer);
      return;
    }
    startBootstrapOnce();
    tries++;
    if (tries >= 80) {
      clearInterval(timer);
      if (!bootstrapStarted) {
        showError('桥未就绪', 'pywebview bridge 没有注入。请重启 app；若仍失败，检查 Content-Security-Policy。');
        setStatus('桥未就绪', 'err');
      }
    }
  }, 250);
});

// ---- header drag (WKWebView doesn't honor -webkit-app-region; bridge to AppKit) ----

function installHeaderDrag() {
  const header = document.querySelector('header');
  if (!header) return;
  const isInteractive = (el) =>
    el && el.closest && el.closest(
      'button, a, input, select, textarea, [role="button"], .dropdown, .modal-overlay'
    );
  header.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (isInteractive(e.target)) return;
    e.preventDefault();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.start_drag) {
      window.pywebview.api.start_drag().catch(() => {});
    }
  });
  // brand-section is inside header but its title text shouldn't block drag
  header.style.cursor = 'default';
}

// ---- version chip + check_update ----

async function initVersionChip() {
  const chip = $('#app-version');
  const updateBtn = $('#btn-update');
  try {
    const info = await window.pywebview.api.hello();
    chip.textContent = 'v' + (info.version || '?');
    chip.title = '当前版本 v' + (info.version || '?');
  } catch (_) {
    chip.textContent = 'v?';
  }
  // background-poll for updates; silent on failure
  try {
    const r = await window.pywebview.api.check_update();
    if (r && r.ok && r.has_update && r.latest) {
      latestUpdateInfo = r;
      chip.textContent = 'v' + (r.current || '?');
      chip.title = `当前版本 v${r.current || '?'} · 新版 v${r.latest} 可用`;
      if (updateBtn) {
        updateBtn.classList.remove('hidden');
        updateBtn.title = `打开 v${r.latest} release`;
      }
    }
  } catch (_) {}
}

async function installAppUpdate() {
  const btn = $('#btn-update');
  if (!latestUpdateInfo || !btn) return;
  btn.disabled = true;
  const previousTitle = btn.title;
  btn.title = `打开 v${latestUpdateInfo.latest} release 页面…`;
  try {
    const url = latestUpdateInfo.html_url || PROJECT_URL;
    const r = await window.pywebview.api.open_url(url);
    if (!r || !r.ok) {
      btn.disabled = false;
      btn.title = (r && r.error) || '打开 release 失败';
      alert((r && r.error) || '打开 release 失败');
      return;
    }
    btn.title = '已打开 release 页面，按 README 手动替换 unsigned app';
  } catch (err) {
    btn.disabled = false;
    btn.title = previousTitle;
    alert(err && err.message ? err.message : String(err));
  }
}

// ---- sidebar toggle ----

const SIDEBAR_KEY = 'sub2cli.sidebar.collapsed';
const SIDEBAR_WIDTH_KEY = 'sub2cli.sidebar.width';
const SIDEBAR_MIN_WIDTH = 184;
const SIDEBAR_MAX_WIDTH = 420;

function clearSidebarSplit() {
  const relay = document.querySelector('.relay-zone');
  const official = document.querySelector('.official-zone');
  if (relay) relay.style.flex = '';
  if (official) official.style.flex = '';
  try { localStorage.removeItem('sub2cli.sidebar.relayRatio'); } catch {}
}

function clampSidebarWidth(width) {
  const viewportMax = Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.floor(window.innerWidth * 0.42)));
  return Math.min(viewportMax, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)));
}

function readSidebarWidth() {
  const raw = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
  return Number.isFinite(raw) && raw > 0 ? clampSidebarWidth(raw) : 240;
}

function applySidebarWidth(width, { persist = true } = {}) {
  const next = clampSidebarWidth(width);
  document.documentElement.style.setProperty('--sidebar-width', `${next}px`);
  const resizer = $('#sidebar-width-resizer');
  if (resizer) {
    resizer.setAttribute('aria-valuemin', String(SIDEBAR_MIN_WIDTH));
    resizer.setAttribute('aria-valuemax', String(clampSidebarWidth(SIDEBAR_MAX_WIDTH)));
    resizer.setAttribute('aria-valuenow', String(next));
  }
  if (persist) localStorage.setItem(SIDEBAR_WIDTH_KEY, String(next));
  return next;
}

function applySidebarState() {
  const collapsed = localStorage.getItem(SIDEBAR_KEY) === '1';
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  if (!collapsed) applySidebarWidth(readSidebarWidth(), { persist: false });
  requestAnimationFrame(clearSidebarSplit);
}
applySidebarState();
$('#btn-sidebar-toggle').addEventListener('click', () => {
  const collapsed = !document.body.classList.contains('sidebar-collapsed');
  localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
  applySidebarState();
});

function initSidebarResizer() {
  clearSidebarSplit();
  const resizer = $('#sidebar-width-resizer');
  const workspace = document.querySelector('.workspace');
  if (!resizer || !workspace) return;
  applySidebarWidth(readSidebarWidth(), { persist: false });
  let active = false;
  let lastWidth = readSidebarWidth();

  const updateFromPointer = (event) => {
    const rect = workspace.getBoundingClientRect();
    lastWidth = applySidebarWidth(event.clientX - rect.left, { persist: false });
  };

  const finish = () => {
    if (!active) return;
    active = false;
    document.body.classList.remove('sidebar-width-resizing');
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(lastWidth));
    window.removeEventListener('pointermove', updateFromPointer);
    window.removeEventListener('pointerup', finish);
    window.removeEventListener('pointercancel', finish);
    try { resizer.releasePointerCapture(resizer._sub2cliPointerId); } catch {}
  };

  resizer.addEventListener('pointerdown', (event) => {
    if (document.body.classList.contains('sidebar-collapsed')) return;
    event.preventDefault();
    active = true;
    resizer._sub2cliPointerId = event.pointerId;
    try { resizer.setPointerCapture(event.pointerId); } catch {}
    document.body.classList.add('sidebar-width-resizing');
    updateFromPointer(event);
    window.addEventListener('pointermove', updateFromPointer);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  });
}
initSidebarResizer();

$('#btn-refresh').addEventListener('click', () => {
  if (!state.bootstrapped) bootstrap();
  else refresh();
});

$('#btn-github').addEventListener('click', openProjectPage);
$('#btn-update').addEventListener('click', installAppUpdate);
$('#brand-link').addEventListener('click', openProjectPage);
$('#brand-link').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    openProjectPage();
  }
});

$('#btn-retry').addEventListener('click', () => {
  if (state.retryRelayDomain) {
    switchRelay(state.retryRelayDomain);
  } else {
    bootstrap();
  }
});

$('#btn-ping-all').addEventListener('click', pingAll);
$('#btn-key-secret-close').addEventListener('click', closeKeySecret);
$('#btn-key-secret-copy').addEventListener('click', copyKeySecret);
$('#key-secret-modal').addEventListener('click', (e) => {
  if (e.target === $('#key-secret-modal')) closeKeySecret();
});

$('#btn-health').addEventListener('click', openHealthModal);
$('#btn-health-close').addEventListener('click', closeHealthModal);
$('#btn-health-rerun').addEventListener('click', runHealthCheck);

$('#health-modal').addEventListener('click', (e) => {
  if (e.target === $('#health-modal')) closeHealthModal();
});

$('#btn-inject').addEventListener('click', openConfigTargetModal);
$('#btn-manage-pool').addEventListener('click', showRoutePoolDashboard);
const routePoolSelect = $('#route-pool-select');
if (routePoolSelect) routePoolSelect.addEventListener('change', () => selectRoutePoolDraft(selectValue('#route-pool-select')));
const routePoolNew = $('#btn-route-pool-new');
if (routePoolNew) routePoolNew.addEventListener('click', createRoutePoolDraft);
const btnRoutePoolToggleAdd = $('#btn-route-pool-toggle-add');
if (btnRoutePoolToggleAdd) btnRoutePoolToggleAdd.addEventListener('click', toggleRoutePoolEditor);
const btnRoutePoolRefresh = $('#btn-route-pool-refresh');
if (btnRoutePoolRefresh) btnRoutePoolRefresh.addEventListener('click', () => refreshRoutePools({ preserveDraft: true }));
const btnRoutePoolRestart = $('#btn-route-pool-restart');
if (btnRoutePoolRestart) btnRoutePoolRestart.addEventListener('click', restartRoutePoolProxy);
const btnRoutePoolSave = $('#btn-route-pool-save');
if (btnRoutePoolSave) btnRoutePoolSave.addEventListener('click', () => saveRoutePool());
const btnRoutePoolAddRoute = $('#btn-route-pool-add-route');
if (btnRoutePoolAddRoute) btnRoutePoolAddRoute.addEventListener('click', addSelectedRouteToPool);
const routePoolSourceType = $('#route-pool-source-type');
if (routePoolSourceType) routePoolSourceType.addEventListener('change', handleRoutePoolSourceTypeChange);
const routePoolRelayDomain = $('#route-pool-relay-domain');
if (routePoolRelayDomain) routePoolRelayDomain.addEventListener('change', handleRoutePoolRelayDomainChange);
const routePoolContextDelete = $('#route-pool-context-delete');
if (routePoolContextDelete) routePoolContextDelete.addEventListener('click', deleteRoutePoolContextRoute);
const routePoolContextMenu = $('#route-pool-context-menu');
if (routePoolContextMenu) {
  routePoolContextMenu.addEventListener('click', (e) => e.stopPropagation());
  routePoolContextMenu.addEventListener('contextmenu', (e) => e.preventDefault());
}
$('#btn-config-target-close').addEventListener('click', closeConfigTargetModal);
const btnConfigPool = $('#btn-config-pool');
if (btnConfigPool) btnConfigPool.addEventListener('click', startPoolConfig);
const btnConfigRelay = $('#btn-config-relay');
if (btnConfigRelay) btnConfigRelay.addEventListener('click', startRelayConfig);
const btnConfigCustom = $('#btn-config-custom');
if (btnConfigCustom) btnConfigCustom.addEventListener('click', startCustomConfig);
$('#btn-config-official').addEventListener('click', startOfficialConfig);
['choice-relay', 'choice-custom', 'choice-codex'].forEach((id) => {
  const select = $('#' + id);
  if (select) select.addEventListener('change', updateConfigChoiceMeta);
});
$('#config-target-modal').addEventListener('click', (e) => {
  if (e.target === $('#config-target-modal')) closeConfigTargetModal();
});
$('#btn-inject-close').addEventListener('click', closeInjectModal);
$('#btn-inject-cancel').addEventListener('click', closeInjectModal);
$('#btn-inject-confirm').addEventListener('click', applyInject);
$('#btn-inject-undo').addEventListener('click', undoInject);
$('#inject-body').addEventListener('wheel', (e) => {
  if (scrollInjectBody(e.deltaY)) e.preventDefault();
}, { passive: false });
$('#inject-body').addEventListener('scroll', updateInjectScrollbar);
window.addEventListener('resize', () => {
  updateInjectScrollbar();
  if (!document.body.classList.contains('sidebar-collapsed')) {
    applySidebarWidth(readSidebarWidth(), { persist: false });
  }
});

$('#btn-account').addEventListener('click', (e) => {
  e.stopPropagation();
  if ($('#account-pop').classList.contains('hidden')) openAccountPop();
  else closeAccountPop();
});
$('#btn-account-add').addEventListener('click', openAddAccountModal);
$('#btn-account-delete').addEventListener('click', deleteCurrentAccount);
$('#btn-add-account-close').addEventListener('click', closeAddAccountModal);
$('#btn-add-account-cancel').addEventListener('click', closeAddAccountModal);
$('#btn-add-account-submit').addEventListener('click', submitAddAccount);
$('#add-account-modal').addEventListener('click', (e) => {
  if (e.target.id === 'add-account-modal') closeAddAccountModal();
});
['add-account-email', 'add-account-password'].forEach((id) => {
  $('#' + id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitAddAccount();
    else if (e.key === 'Escape') closeAddAccountModal();
  });
});
$('#btn-add-codex-account').addEventListener('click', openAddCodexAccountModal);
$('#btn-add-codex-close').addEventListener('click', closeAddCodexAccountModal);
$('#btn-add-codex-cancel').addEventListener('click', closeAddCodexAccountModal);
$('#btn-add-codex-submit').addEventListener('click', submitAddCodexAccount);
$('#add-codex-account-modal').addEventListener('click', (e) => {
  if (e.target.id === 'add-codex-account-modal') closeAddCodexAccountModal();
});
['add-codex-slot', 'add-codex-display'].forEach((id) => {
  $('#' + id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitAddCodexAccount();
    else if (e.key === 'Escape') closeAddCodexAccountModal();
  });
});
document.addEventListener('click', (e) => {
  closeRoutePoolContextMenu();
  const pop = $('#account-pop');
  if (pop.classList.contains('hidden')) return;
  if (!pop.contains(e.target) && e.target.id !== 'btn-account') closeAccountPop();
});

$('#inject-modal').addEventListener('click', (e) => {
  if (e.target === $('#inject-modal')) closeInjectModal();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeRoutePoolContextMenu();
    if (!$('#health-modal').classList.contains('hidden')) closeHealthModal();
    if (!$('#config-target-modal').classList.contains('hidden')) closeConfigTargetModal();
    if (!$('#inject-modal').classList.contains('hidden')) closeInjectModal();
    if (!$('#add-codex-account-modal').classList.contains('hidden')) closeAddCodexAccountModal();
  }
});

clearModeState();

$('#btn-test-selected-groups').addEventListener('click', testSelectedGroups);

// ---- add-relay modal (probe-first stepped flow) ----

let addRelayStage = 'probe';  // 'probe' | 'edge' | 'creds'

function openAddRelayModal() {
  $('#add-relay-url').value = '';
  $('#add-relay-email').value = '';
  $('#add-relay-password').value = '';
  resetAddRelayUI();
  $('#add-relay-modal').classList.remove('hidden');
  setTimeout(() => $('#add-relay-url').focus(), 50);
}

function closeAddRelayModal() {
  $('#add-relay-modal').classList.add('hidden');
}

function resetAddRelayUI() {
  addRelayStage = 'probe';
  $('#add-relay-probe-result').classList.add('hidden');
  $('#add-relay-probe-result').className = 'probe-result hidden';
  $('#add-relay-probe-result').textContent = '';
  $('#add-relay-email-row').classList.add('hidden');
  $('#add-relay-pw-row').classList.add('hidden');
  $('#add-relay-creds-hint').classList.add('hidden');
  $('#add-relay-error').classList.add('hidden');
  $('#add-relay-error').textContent = '';
  $('#add-relay-url').disabled = false;
  const btn = $('#btn-add-relay-action');
  btn.disabled = false;
  btn.textContent = '检测';
}

async function actionAddRelay() {
  if (addRelayStage === 'probe') {
    return probeRelay();
  }
  return submitAddRelay();
}

async function probeRelay() {
  const url = $('#add-relay-url').value.trim();
  const errBox = $('#add-relay-error');
  errBox.classList.add('hidden');
  if (!url) {
    errBox.textContent = 'URL 必填';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#btn-add-relay-action');
  btn.disabled = true;
  btn.textContent = '检测中…';
  try {
    const r = await window.pywebview.api.probe_relay(url);
    if (!r || !r.ok) {
      errBox.textContent = (r && r.error) || '检测失败';
      errBox.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = '检测';
      return;
    }
    const result = $('#add-relay-probe-result');
    if (r.has_edge_session && r.edge_email) {
      addRelayStage = 'edge';
      result.className = 'probe-result probe-success';
      result.textContent = `✓ 检测到浏览器已登录 ${r.edge_email}`;
      result.classList.remove('hidden');
      $('#add-relay-url').disabled = true;
      btn.textContent = '添加';
      btn.disabled = false;
    } else if (r.turnstile) {
      // stay in probe stage so user can re-click after browser login
      addRelayStage = 'probe';
      result.className = 'probe-result probe-warn';
      result.textContent = '⚠ 该站点启用了 Cloudflare Turnstile 人机验证。请先在浏览器登录该站, 再点重新检测。';
      result.classList.remove('hidden');
      btn.textContent = '重新检测';
      btn.disabled = false;
    } else {
      addRelayStage = 'creds';
      result.className = 'probe-result probe-info';
      result.textContent = '✓ 站点可用。';
      result.classList.remove('hidden');
      $('#add-relay-email-row').classList.remove('hidden');
      $('#add-relay-pw-row').classList.remove('hidden');
      $('#add-relay-creds-hint').classList.remove('hidden');
      $('#add-relay-url').disabled = true;
      btn.textContent = '添加';
      btn.disabled = false;
      setTimeout(() => $('#add-relay-email').focus(), 50);
    }
  } catch (err) {
    errBox.textContent = err && err.message ? err.message : String(err);
    errBox.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '检测';
  }
}

async function submitAddRelay() {
  const url = $('#add-relay-url').value.trim();
  const email = (addRelayStage === 'creds') ? $('#add-relay-email').value.trim() : '';
  const password = (addRelayStage === 'creds') ? $('#add-relay-password').value : '';
  const errBox = $('#add-relay-error');
  errBox.classList.add('hidden');
  errBox.textContent = '';
  if (addRelayStage === 'creds' && ((email && !password) || (!email && password))) {
    errBox.textContent = 'email 和密码要么都填要么都不填';
    errBox.classList.remove('hidden');
    return;
  }
  const btn = $('#btn-add-relay-action');
  btn.disabled = true;
  btn.textContent = '添加中…';
  try {
    const r = await window.pywebview.api.add_relay(url, email, password);
    if (!r || !r.ok) {
      errBox.textContent = (r && r.error) || '添加失败';
      errBox.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = '添加';
      return;
    }
    closeAddRelayModal();
    applyBootstrap(r);
    setStatus('✓ 已连接', 'ok');
    showScreen('dashboard');
  } catch (err) {
    errBox.textContent = err && err.message ? err.message : String(err);
    errBox.classList.remove('hidden');
    btn.disabled = false;
    btn.textContent = '添加';
  }
}

$('#btn-add-relay').addEventListener('click', openAddRelayModal);
$('#btn-add-relay-close').addEventListener('click', closeAddRelayModal);
$('#btn-add-relay-cancel').addEventListener('click', closeAddRelayModal);
$('#btn-add-relay-action').addEventListener('click', actionAddRelay);
$('#add-relay-url').addEventListener('input', () => {
  // url edited → reset probe state, force re-probe
  if (addRelayStage !== 'probe') resetAddRelayUI();
});
$('#add-relay-modal').addEventListener('click', (e) => {
  if (e.target.id === 'add-relay-modal') closeAddRelayModal();
});
['add-relay-url', 'add-relay-email', 'add-relay-password'].forEach((id) => {
  $('#' + id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') actionAddRelay();
    else if (e.key === 'Escape') closeAddRelayModal();
  });
});

// ---- custom API: sidebar + add modal + dashboard bindings ----
$('#btn-add-custom-api').addEventListener('click', openAddCustomApiModal);
$('#btn-add-custom-close').addEventListener('click', closeAddCustomApiModal);
$('#btn-add-custom-cancel').addEventListener('click', closeAddCustomApiModal);
$('#btn-add-custom-submit').addEventListener('click', submitAddCustomApi);
$('#add-custom-api-modal').addEventListener('click', (e) => {
  if (e.target.id === 'add-custom-api-modal') closeAddCustomApiModal();
});
['add-custom-url', 'add-custom-key', 'add-custom-name'].forEach((id) => {
  $('#' + id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitAddCustomApi();
    else if (e.key === 'Escape') closeAddCustomApiModal();
  });
});
$('#btn-custom-add-model').addEventListener('click', addCustomModelRow);
$('#btn-custom-test-all').addEventListener('click', () => testCustomApiModels(state.customApiColumns));
$('#btn-custom-refresh-models').addEventListener('click', () => {
  if (state.currentCustomApi) loadCustomApiModels(state.currentCustomApi.id);
});
