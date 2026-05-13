export async function apiFetch(path, init) {
  const r = await fetch(path, init);
  return r;
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
