// 前端公共辅助：API 调用、转义、时间格式化、负责人凭证本地保存。

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    throw new Error((data && data.detail) || `请求失败（${res.status}）`);
  }
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function pad(n) { return String(n).padStart(2, '0'); }

function fmtTime(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtCountdown(remainMs) {
  if (remainMs <= 0) return '已截止';
  const s = Math.floor(remainMs / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return (d ? `${d}天 ` : '') + `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

function showErr(el, text) {
  el.innerHTML = `<div class="callout err">${esc(text)}</div>`;
}

function showInfo(el, text, kind = 'warn') {
  el.innerHTML = `<div class="callout ${kind}">${esc(text)}</div>`;
}

function saveTest(code, token) {
  const list = JSON.parse(localStorage.getItem('sniff_tests') || '[]');
  const rest = list.filter((t) => t.code !== code);
  rest.unshift({ code, token, saved_at: Date.now() });
  localStorage.setItem('sniff_tests', JSON.stringify(rest.slice(0, 20)));
}
