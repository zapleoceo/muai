import { apiFetch, esc } from '../api.js';

let _status = 'new';

export function onImprovementStatus(status) {
  _status = status || 'new';
  loadImprovements();
}

export async function loadImprovements() {
  const box = document.getElementById('improvements-box');
  const sel = document.getElementById('impr-status');
  if (sel) sel.value = _status;
  if (!box) return;
  box.textContent = 'Загрузка…';

  const r = await apiFetch(`/api/admin/router-suggestions?status=${encodeURIComponent(_status)}&limit=200`);
  if (!r.ok) {
    box.textContent = 'Ошибка загрузки';
    return;
  }
  const rows = await r.json();
  if (!rows.length) {
    box.innerHTML = '<p style="color:#475569;font-size:0.85rem">Пока нет предложений.</p>';
    return;
  }

  box.innerHTML = rows.map(renderSuggestion).join('');
}

function renderSuggestion(s) {
  const created = s.created_at ? s.created_at.slice(0, 19).replace('T', ' ') : '—';
  const status = esc(s.status || '—');
  const title = esc(truncate(s.query || '', 120) || '—');
  const rule = esc(s.proposed_rule || '—');
  const currentPlan = s.current_plan ? esc(JSON.stringify(s.current_plan, null, 2)) : '—';
  const proposedPlan = s.proposed_plan ? esc(JSON.stringify(s.proposed_plan, null, 2)) : '—';
  const canApprove = s.status !== 'approved';
  const canReject = s.status !== 'rejected';

  return `
    <details class="section" style="margin-top:12px">
      <summary class="section-header">
        <h2 style="font-size:0.95rem">${title}</h2>
        <div class="section-actions" style="gap:8px">
          <span style="color:#64748b;font-size:0.8rem">#${s.id} • ${created} • ${status}</span>
          <button class="btn btn-sm" ${canApprove ? '' : 'disabled'} onclick="event.preventDefault();event.stopPropagation();approveImprovement(${s.id})">Одобрить</button>
          <button class="btn btn-sm btn-danger" ${canReject ? '' : 'disabled'} onclick="event.preventDefault();event.stopPropagation();rejectImprovement(${s.id})">Отклонить</button>
        </div>
      </summary>
      <div style="padding-top:10px">
        <div style="color:#94a3b8;font-size:0.8rem;margin-bottom:6px">Правило</div>
        <div style="white-space:pre-wrap;font-size:0.9rem">${rule}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
          <div>
            <div style="color:#94a3b8;font-size:0.8rem;margin-bottom:6px">Текущий план (как было)</div>
            <pre style="max-height:260px;overflow:auto;background:#0f1117;padding:10px;border-radius:8px;font-size:0.8rem">${currentPlan}</pre>
          </div>
          <div>
            <div style="color:#94a3b8;font-size:0.8rem;margin-bottom:6px">Предложенный план (как надо)</div>
            <pre style="max-height:260px;overflow:auto;background:#0f1117;padding:10px;border-radius:8px;font-size:0.8rem">${proposedPlan}</pre>
          </div>
        </div>
      </div>
    </details>
  `;
}

export async function approveImprovement(id) {
  const r = await apiFetch(`/api/admin/router-suggestions/${id}/approve`, { method: 'POST' });
  if (!r.ok) alert('Ошибка подтверждения');
  loadImprovements();
}

export async function rejectImprovement(id) {
  const r = await apiFetch(`/api/admin/router-suggestions/${id}/reject`, { method: 'POST' });
  if (!r.ok) alert('Ошибка отклонения');
  loadImprovements();
}

function truncate(s, n) {
  const str = String(s || '');
  if (str.length <= n) return str;
  return str.slice(0, n - 1) + '…';
}
