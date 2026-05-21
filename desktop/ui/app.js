const $ = (sel) => document.querySelector(sel);

function setOutput(text, cls = '') {
  const el = $('#output');
  el.textContent = text;
  el.className = 'output' + (cls ? ' ' + cls : '');
}

window.addEventListener('pywebviewready', () => {
  const s = $('#status');
  s.textContent = '✓ 桥已就绪';
  s.style.color = 'var(--success)';
});

$('#btn-hello').addEventListener('click', async () => {
  setOutput('正在调用 JsApi.hello()…');
  try {
    const data = await window.pywebview.api.hello();
    setOutput(JSON.stringify(data, null, 2), 'ok');
  } catch (err) {
    setOutput('错误: ' + (err && err.message ? err.message : String(err)), 'err');
  }
});

$('#btn-relays').addEventListener('click', async () => {
  setOutput('正在调用 JsApi.list_relays()…');
  try {
    const relays = await window.pywebview.api.list_relays();
    if (!relays.length) {
      setOutput('(尚未保存 relay; 运行 ../sub2cli 进 REPL 跑首次配置)', 'ok');
    } else {
      setOutput('已保存的 relay:\n' + relays.map((d, i) => `  ${i + 1}. ${d}`).join('\n'), 'ok');
    }
  } catch (err) {
    setOutput('错误: ' + (err && err.message ? err.message : String(err)), 'err');
  }
});
