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
  groupSelected: {}, // group_id → boolean (checkbox state for batch test)
  lastDomain: null,  // current relay domain, for modal hints
  currentEmail: null,
  lastInjectBackup: null,
  lastInjectChanges: null,
};

const PROJECT_URL = 'https://github.com/r266-tech/sub2cli';

// ---- screen routing ----

function showScreen(id) {
  for (const s of ['loading', 'error', 'dashboard']) {
    $(`#${s}`).classList.toggle('hidden', s !== id);
  }
}

function setStatus(_text, cls = '') {
  const chip = $('#brand-subtitle');
  if (!chip) return;
  if (cls) chip.dataset.state = cls;
}

function setBrandSubtitle(text) {
  const body = $('#brand-subtitle-text');
  if (!body) return;
  body.textContent = text ? `已连接 ${text}` : '等待 relay';
}

function showError(title, msg) {
  $('#error-title').textContent = title;
  $('#error-msg').textContent = msg || '(空)';
  showScreen('error');
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
    span.textContent = `[OK] ${fmtLatency(result.latency_ms)}`;
    return span;
  }
  span.classList.add('err');
  span.textContent = `[ERR] ${result.status ?? '?'}`;
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

// ---- one-mode UI ----

function clearModeState() {
  document.body.classList.remove('simple-mode');
  try { localStorage.removeItem('sub2cli.simple_mode'); } catch {}
}

// ---- bootstrap ----

async function bootstrap(autoRecovered = false) {
  showScreen('loading');
  setStatus('连接中…');
  refreshSidebar();
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
        showError('请重新登录', `请先在浏览器登录 ${data.domain || '当前 relay'}`);
      } else if (data.needs_setup) {
        showError('尚未配置', data.error || '未知错误');
      } else {
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
    const data = await window.pywebview.api.refresh();
    if (!data.ok) {
      setStatus('刷新失败', 'err');
      if (data.needs_login) {
        showError('请重新登录', `请先在浏览器登录 ${data.domain || '当前 relay'}`);
      } else {
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
  state.groups = data.groups || [];
  state.defaultKey = data.default_key;
  state.defaultEndpoint = data.default_endpoint;
  setBrandSubtitle(data.site || data.domain || '');
  renderAccount(data);
  renderDefaultKey(data);
  renderEndpoints(data.endpoints || [], data.default_endpoint);
  renderGroups(data.groups || [], data.keys || [], data.default_key);
  refreshSidebar();  // sidebar is independent of bootstrap data
  updateAccountChip((data.user && data.user.email) || null);
}

function updateAccountChip(email) {
  state.currentEmail = email || null;
  $('#account-email').textContent = email || '未设';
  updateAccountDeleteButton(email || null);
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
      const item = el('div', { className: 'relay-item' + (relay.is_current ? ' active' : '') });
      const info = el('div', { className: 'relay-info' });
      info.appendChild(el('span', { className: 'relay-domain', text: relay.site || relay.domain }));
      item.appendChild(info);
      item.appendChild(el('div', { className: 'relay-indicator' }));
      if (!relay.is_current) {
        item.style.cursor = 'pointer';
        item.addEventListener('click', () => switchRelay(relay.domain));
      }
      item.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        if (confirm(`删除中转站 ${relay.site || relay.domain}?\n会清掉它在 Keychain 里的 token 和密码 (不可恢复).`)) {
          removeRelay(relay.domain);
        }
      });
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
      showError('请添加中转', '已删除最后一个中转, 点 "+ 新增中转" 添加');
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
    const data = await window.pywebview.api.switch_relay(domain);
    if (!data.ok) {
      if (data.needs_login) {
        showError('请重新登录', `请先在浏览器登录 ${data.domain || domain}`);
      } else {
        showError('切 relay 失败', data.error);
      }
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
    const r = await window.pywebview.api.list_accounts();
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

async function switchAccount(email) {
  setStatus(`切到 ${email}…`, 'warn');
  closeAccountPop();
  showScreen('loading');
  try {
    const data = await window.pywebview.api.switch_account(email);
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
    const r = await window.pywebview.api.delete_account(email);
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

// ---- inject modal (dry-run gate + apply) ----

function openInjectModal() {
  $('#inject-modal').classList.remove('hidden');
  state.lastInjectBackup = null;
  state.lastInjectChanges = null;
  const undoBtn = $('#btn-inject-undo');
  undoBtn.classList.add('hidden');
  undoBtn.disabled = true;
  const confirmBtn = $('#btn-inject-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '开始注入';
  loadInjectPlan();
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
    children.push(el('pre', { className: 'plan-preview dryrun-log', text: options.log }));
  }
  body.replaceChildren(...children);
  requestAnimationFrame(updateInjectScrollbar);
}

async function loadInjectPlan() {
  state.lastInjectBackup = null;
  $('#btn-inject-undo').classList.add('hidden');
  $('#btn-inject-undo').disabled = true;
  renderInjectState('正在生成注入计划', [
    { state: 'ok', title: '读取当前默认配置', detail: '使用当前默认 key 和端点作为注入目标' },
    { state: 'running', title: '执行 dry-run', detail: '扫描 Codex 配置和历史 session, 只计算改动, 不写入文件' },
    { state: 'pending', title: '等待确认', detail: '计划通过后才允许写入 Codex' },
  ]);
  try {
    const r = await window.pywebview.api.inject_plan();
    if (!r.ok) {
      state.lastInjectChanges = null;
      renderInjectState('注入计划生成失败', [
        { state: 'ok', title: '读取当前默认配置', detail: '已完成' },
        { state: 'err', title: 'dry-run 失败', detail: r.error || '未知错误' },
        { state: 'pending', title: '未写入', detail: '没有修改 Codex 配置' },
      ], {
        logClass: 'plan-result err',
        logTitle: '[ERR] 详细错误',
        log: planLog([
          `[ERR] ${r.error || '未知错误'}`,
          r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
          r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
        ]),
      });
      $('#btn-inject-confirm').textContent = '[ERR] 不可注入';
      $('#btn-inject-confirm').disabled = true;
      return;
    }
    state.lastInjectChanges = r.changes || [];
    renderInjectState('注入计划已就绪', [
      { state: 'ok', title: '读取当前默认配置', detail: r.label || '已完成' },
      { state: 'ok', title: 'dry-run 通过', detail: 'sub2cli-inject 已确认可执行' },
      { state: 'running', title: '等待确认写入', detail: '点击确认后才会修改 Codex 配置并生成回滚备份' },
    ], {
      changes: r.changes || [],
      logTitle: '[CHECK] Dry-run 原始输出',
      log: planLog([
        r.command ? `[SYS] ${r.command}` : null,
        r.plan_text || '(空)',
      ]),
    });
    $('#btn-inject-confirm').textContent = '开始注入';
    $('#btn-inject-confirm').disabled = false;
  } catch (err) {
    renderInjectState('注入计划调用失败', [
      { state: 'err', title: '调用失败', detail: err && err.message ? err.message : String(err) },
      { state: 'pending', title: '未写入', detail: '没有修改 Codex 配置' },
    ]);
    state.lastInjectChanges = null;
    $('#btn-inject-confirm').textContent = '[ERR] 不可注入';
    $('#btn-inject-confirm').disabled = true;
  }
}

async function applyInject() {
  const confirmBtn = $('#btn-inject-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = '执行中…';
  $('#btn-inject-undo').classList.add('hidden');
  $('#btn-inject-undo').disabled = true;
  setStatus('注入中…', 'warn');
  renderInjectState('正在注入 Codex', [
    { state: 'ok', title: 'dry-run 已确认', detail: '计划已经通过' },
    { state: 'running', title: '写入 Codex 配置', detail: '保存 API 渠道、切换 auth.json、更新 config.toml' },
    { state: 'pending', title: '刷新 Codex App', detail: '写入成功后会按 sub2cli-inject 逻辑重启/打开 Codex' },
    { state: 'pending', title: '准备撤销点', detail: '成功后会显示撤销本次注入按钮' },
  ], {
    changes: state.lastInjectChanges || [
      { path: '~/.codex/provider-slots.json', detail: '新增或更新当前中转的 API 渠道' },
      { path: '~/.codex/auth.json', detail: '切换到该渠道的 API key 登录文件' },
      { path: '~/.codex/config.toml', detail: '写入 Codex provider 的 base_url/model 配置' },
      { path: '~/Library/Application Support/Codex', detail: '保持或切换 Codex App profile symlink' },
      { path: 'Codex 历史 session', detail: '必要时归一历史会话 provider, 让旧会话继续走当前渠道' },
      { path: 'Codex App', detail: '需要时重新打开以加载新配置' },
    ],
  });
  try {
    const r = await window.pywebview.api.inject_apply();
    state.lastInjectBackup = r.backup_name || null;
    renderInjectState(r.ok ? '注入完成' : '注入失败', [
      { state: 'ok', title: 'dry-run 已确认', detail: '计划已经通过' },
      {
        state: r.ok ? 'ok' : 'err',
        title: r.ok ? '写入 Codex 配置完成' : '写入 Codex 配置失败',
        detail: r.ok ? 'sub2cli-inject 返回 rc=0' : `sub2cli-inject 返回 rc=${r.returncode ?? '?'}`,
      },
      {
        state: r.ok ? 'ok' : 'pending',
        title: '刷新 Codex App',
        detail: r.ok ? '已按注入器输出处理' : '未完成',
      },
      {
        state: r.ok && r.backup_name ? 'ok' : (r.ok ? 'warn' : 'pending'),
        title: '撤销点',
        detail: r.backup_name ? `可回滚到 ${r.backup_name}` : (r.ok ? '未从输出中识别到回滚备份名' : '失败时不提供撤销'),
      },
    ], {
      logClass: r.ok ? 'plan-result ok' : 'plan-result err',
      logTitle: r.ok ? '[READY] 注入输出' : '[ERR] 注入输出',
      log: planLog([
        r.ok ? '[READY] Injection completed.' : `[ERR] Injection failed (rc=${r.returncode ?? '?'})`,
        r.rollback_command ? `[INFO] 撤销命令: ${r.rollback_command}` : null,
        r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
        r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
      ]),
    });
    if (r.ok && r.backup_name) {
      const undoBtn = $('#btn-inject-undo');
      undoBtn.classList.remove('hidden');
      undoBtn.disabled = false;
    }
    confirmBtn.textContent = r.ok ? '[READY] 完成' : '[ERR] 失败';
    setStatus(r.ok ? '✓ 注入完成' : '✗ 注入失败', r.ok ? 'ok' : 'err');
  } catch (err) {
    setStatus('错误', 'err');
    confirmBtn.textContent = '[ERR] 失败';
    renderInjectState('注入调用失败', [
      { state: 'err', title: '调用失败', detail: err && err.message ? err.message : String(err) },
      { state: 'pending', title: '撤销点', detail: '未完成, 不提供撤销' },
    ]);
  }
}

async function undoInject() {
  const backupName = state.lastInjectBackup;
  if (!backupName) return;
  const undoBtn = $('#btn-inject-undo');
  undoBtn.disabled = true;
  $('#btn-inject-confirm').disabled = true;
  setStatus('撤销注入中…', 'warn');
  renderInjectState('正在撤销本次注入', [
    { state: 'running', title: '恢复备份', detail: `回滚到 ${backupName}` },
    { state: 'pending', title: '恢复 Codex 配置', detail: '恢复 auth.json/config.toml/App profile 等状态' },
  ]);
  try {
    const r = await window.pywebview.api.inject_rollback(backupName);
    renderInjectState(r.ok ? '已撤销本次注入' : '撤销失败', [
      { state: r.ok ? 'ok' : 'err', title: '恢复备份', detail: r.ok ? `已恢复 ${backupName}` : `rollback 返回 rc=${r.returncode ?? '?'}` },
      { state: r.ok ? 'ok' : 'pending', title: 'Codex 配置', detail: r.ok ? '已恢复到注入前状态' : '未确认恢复' },
    ], {
      logClass: r.ok ? 'plan-result ok' : 'plan-result err',
      logTitle: r.ok ? '[READY] 撤销输出' : '[ERR] 撤销输出',
      log: planLog([
        r.stdout ? `[INFO] stdout:\n${r.stdout}` : null,
        r.stderr ? `[INFO] stderr:\n${r.stderr}` : null,
      ]),
    });
    setStatus(r.ok ? '✓ 已撤销注入' : '✗ 撤销失败', r.ok ? 'ok' : 'err');
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
  }
}

// ---- renderers ----

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
  // badge top-right: "ACTIVE" / status
  const badge = $('#acc-status-badge');
  if (badge) {
    badge.textContent = status.toUpperCase();
    badge.className = 'badge-label';
    if (status !== 'active' && status !== '—') badge.classList.add('err');
  }
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
  const revealBtn = $('#btn-reveal');
  revealBtn.disabled = false;
  revealBtn.textContent = '显示';
  revealBtn.dataset.revealed = '0';
  const ep = data.default_endpoint;
  $('#ep-current').textContent = ep ? ep.endpoint : '—';
}

function endpointStatusCell(isCurrent, pingResult) {
  let circleClass = isCurrent ? 'good' : 'medium';
  let labelClass = isCurrent ? 'status-checked' : 'status-dim';
  let label = isCurrent ? '[ON]' : '[IDLE]';

  if (pingResult) {
    if (pingResult.running) {
      circleClass = 'medium';
      labelClass = 'status-running';
      label = '[PING]';
    } else if (pingResult.ok) {
      circleClass = 'good';
      labelClass = 'status-checked';
      label = '[OK]';
    } else {
      circleClass = 'bad';
      labelClass = 'status-failed';
      label = '[ERR]';
    }
  }

  return el('div', {
    className: 'ping-indicator',
    children: [
      el('span', { className: `indicator-circle ${circleClass}` }),
      el('span', { className: labelClass, text: label }),
    ],
  });
}

function buildEndpointRow(ep, isCurrent, pingResult) {
  const tr = document.createElement('tr');
  if (isCurrent) tr.classList.add('current');

  tr.appendChild(el('td', {
    children: [endpointStatusCell(isCurrent, pingResult)],
  }));
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
      text: '[选用]',
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

  const checkboxTd = el('td', { className: 'checkbox-col checkbox-custom-parent' });
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'checkbox-custom';
  checkbox.checked = !!state.groupSelected[g.id];
  checkbox.addEventListener('change', () => {
    state.groupSelected[g.id] = checkbox.checked;
    updateBatchTestButton();
    updateToggleAllButton();
  });
  checkboxTd.appendChild(checkbox);
  tr.appendChild(checkboxTd);

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
    text: '[测试]',
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
  updateBatchTestButton();
  updateToggleAllButton();
}

// ---- batch group test (checkbox + parallel) ----

function getSelectedGroupIds() {
  return state.groups.filter((g) => state.groupSelected[g.id]).map((g) => g.id);
}

function updateBatchTestButton() {
  const btn = $('#btn-test-selected-groups');
  if (!btn) return;
  const n = getSelectedGroupIds().length;
  btn.disabled = n === 0;
  btn.textContent = `测试选中 (${n})`;
}

function updateToggleAllButton() {
  const btn = $('#btn-toggle-all-groups');
  if (!btn) return;
  const total = state.groups.length;
  const sel = getSelectedGroupIds().length;
  btn.textContent = (total > 0 && sel === total) ? '反选' : '全选';
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
  const ids = getSelectedGroupIds();
  if (!ids.length) return;
  // SERIAL: server-side "current default group" is per-account state;
  // parallel calls would all race on the switch and end up hitting whichever
  // group landed last. Must do switch → send → await → next, one at a time.
  for (const id of ids) {
    state.groupResults[id] = { chat: { running: true }, image: { running: true } };
  }
  renderGroups(state.groups, [], state.defaultKey);
  let done = 0;
  for (const id of ids) {
    setStatus(`串行测试 ${done + 1}/${ids.length}…`, 'warn');
    try {
      const r = await window.pywebview.api.test_group(id);
      if (!r.ok) {
        state.groupResults[id] = {
          chat: { ok: false, status: 'err', summary: r.error },
          image: { ok: false, status: 'err', summary: r.error },
        };
      } else {
        state.groupResults[id] = { chat: r.chat, image: r.image };
        if (r.default_key) state.defaultKey = r.default_key;
      }
    } catch (err) {
      state.groupResults[id] = {
        chat: { ok: false, status: 'err', summary: String(err) },
        image: { ok: false, status: 'err', summary: String(err) },
      };
    }
    done++;
    renderGroups(state.groups, [], state.defaultKey);  // 每个完成立刻刷
  }
  setStatus(`✓ 完成 ${done}/${ids.length} 个分组`, 'ok');
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
  const btn = $('#btn-reveal');
  const keyEl = $('#key-value');
  if (btn.dataset.revealed === '1') {
    keyEl.textContent = (state.defaultKey && state.defaultKey.key_masked) || '—';
    btn.textContent = '显示';
    btn.dataset.revealed = '0';
    return;
  }
  try {
    const r = await window.pywebview.api.reveal_default_key();
    if (!r.ok) {
      setStatus(r.error, 'err');
      return;
    }
    keyEl.textContent = r.key;
    btn.textContent = '隐藏';
    btn.dataset.revealed = '1';
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
  // macOS: kill native white titlebar, blend with our dark header
  if (window.pywebview && window.pywebview.api && window.pywebview.api.customize_chrome) {
    window.pywebview.api.customize_chrome().catch(() => {});
  }
  installHeaderDrag();
  bootstrap();
  initVersionChip();
});

// ---- header drag (WKWebView doesn't honor -webkit-app-region; bridge to AppKit) ----

function installHeaderDrag() {
  const header = document.querySelector('header');
  if (!header) return;
  const isInteractive = (el) =>
    el && el.closest && el.closest(
      'button, a, input, select, textarea, [role="button"], .dropdown, .modal-overlay, .version-chip.has-update'
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
    if (r && r.ok && r.has_update && r.latest && r.html_url) {
      chip.textContent = `v${r.current} → v${r.latest}`;
      chip.classList.add('has-update');
      chip.title = `新版 v${r.latest} 可用 · 点击打开 GitHub 项目`;
      chip.addEventListener('click', (e) => {
        e.stopPropagation();
        openProjectPage();
      });
    }
  } catch (_) {}
}

// ---- sidebar toggle ----

const SIDEBAR_KEY = 'sub2cli.sidebar.collapsed';
function applySidebarState() {
  const collapsed = localStorage.getItem(SIDEBAR_KEY) === '1';
  document.body.classList.toggle('sidebar-collapsed', collapsed);
}
applySidebarState();
$('#btn-sidebar-toggle').addEventListener('click', () => {
  const collapsed = !document.body.classList.contains('sidebar-collapsed');
  localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
  applySidebarState();
});

$('#btn-refresh').addEventListener('click', () => {
  if (!state.bootstrapped) bootstrap();
  else refresh();
});
$('#btn-github').addEventListener('click', openProjectPage);
$('#brand-link').addEventListener('click', openProjectPage);
$('#brand-link').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    openProjectPage();
  }
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
$('#btn-inject-undo').addEventListener('click', undoInject);
$('#inject-body').addEventListener('wheel', (e) => {
  if (scrollInjectBody(e.deltaY)) e.preventDefault();
}, { passive: false });
$('#inject-body').addEventListener('scroll', updateInjectScrollbar);
window.addEventListener('resize', updateInjectScrollbar);

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
document.addEventListener('click', (e) => {
  const pop = $('#account-pop');
  if (pop.classList.contains('hidden')) return;
  if (!pop.contains(e.target) && e.target.id !== 'btn-account') closeAccountPop();
});

$('#inject-modal').addEventListener('click', (e) => {
  if (e.target === $('#inject-modal')) closeInjectModal();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!$('#health-modal').classList.contains('hidden')) closeHealthModal();
    if (!$('#inject-modal').classList.contains('hidden')) closeInjectModal();
  }
});

clearModeState();

$('#btn-test-selected-groups').addEventListener('click', testSelectedGroups);
$('#btn-toggle-all-groups').addEventListener('click', toggleAllGroups);

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
