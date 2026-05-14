export async function apiFetch(path, init) {
  const r = await fetch(path, init);
  return r;
}

/** Disable a button and show spinner while async fn runs. Returns fn result. */
export async function withBtn(el, fn) {
  if (!el || el.disabled) return fn();
  const orig = el.innerHTML;
  el.disabled = true;
  el.innerHTML = '<span class="btn-spinner">⏳</span>';
  try {
    return await fn();
  } finally {
    el.disabled = false;
    el.innerHTML = orig;
  }
}

/** Find element by selector and run withBtn on it. */
export async function withBtnId(id, fn) {
  return withBtn(document.getElementById(id), fn);
}

export function fmt(n) {
  return n?.toLocaleString('ru') ?? '—';
}

export function pct(a, b) {
  return b ? Math.round(a / b * 100) : 0;
}

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
