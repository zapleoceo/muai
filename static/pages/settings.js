import { apiFetch } from '../api.js';

const TYPE_LABELS = { private: 'Личные', group: 'Группы', supergroup: 'Супергруппы', channel: 'Каналы' };
let _settings = {};
let _tokens = [];

export function initSettingsPage() {
  const providerSelect = document.getElementById('inp-token-provider');
  if (providerSelect) {
    providerSelect.addEventListener('change', onProviderChange);
    onProviderChange();
  }
}

export async function loadSettings() {
  const r = await apiFetch('/api/admin/settings/sync');
  if (!r.ok) return;
  _settings = await r.json();
  renderSettings();
}

function renderSettings() {
  const allowed = _settings.allowed_types || [];
  document.getElementById('type-grid').innerHTML =
    Object.entries(TYPE_LABELS).map(([t, label]) => `
      <label class=\"type-toggle ${allowed.includes(t) ? 'on' : ''}\" onclick=\"toggleType('${t}',this)\">
        <input type=\"checkbox\" ${allowed.includes(t) ? 'checked' : ''} onchange=\"toggleType('${t}',this.closest('.type-toggle'))\">
        <span>${label}</span>
      </label>
    `).join('');

  document.getElementById('inp-depth').value = _settings.default_depth_days || 7;
  renderBlacklist();
}

export function toggleType(type, el) {
  const allowed = _settings.allowed_types || [];
  const idx = allowed.indexOf(type);
  if (idx === -1) { allowed.push(type); el.classList.add('on'); }
  else { allowed.splice(idx, 1); el.classList.remove('on'); }
  _settings.allowed_types = allowed;
  el.querySelector('input').checked = !el.querySelector('input').checked;
}

function renderBlacklist() {
  const bl = _settings.blacklist || [];
  document.getElementById('bl-tags').innerHTML = bl.map((item, i) => `
    <span class=\"bl-tag\">${item} <button onclick=\"removeBlacklist(${i})\">×</button></span>
  `).join('');
}

export function addBlacklist() {
  const val = document.getElementById('inp-bl').value.trim();
  if (!val) return;
  const bl = _settings.blacklist || [];
  const parsed = /^-?\d+$/.test(val) ? parseInt(val) : val;
  if (!bl.includes(parsed)) bl.push(parsed);
  _settings.blacklist = bl;
  document.getElementById('inp-bl').value = '';
  renderBlacklist();
}

export function removeBlacklist(i) {
  _settings.blacklist.splice(i, 1);
  renderBlacklist();
}

export async function saveSettings() {
  _settings.default_depth_days = parseInt(document.getElementById('inp-depth').value) || 7;
  const r = await apiFetch('/api/admin/settings/sync', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(_settings),
  });
  if (r.ok) {
    _settings = await r.json();
    renderSettings();
    alert('Сохранено');
  } else {
    alert('Ошибка сохранения');
  }
}

export function onProviderChange() {
  const provider = document.getElementById('inp-token-provider').value;
  const chat = document.getElementById('cap-chat');
  const embed = document.getElementById('cap-embed');
  const embedMedia = document.getElementById('cap-embed-media');
  if (provider === 'voyage') {
    chat.checked = false;
    embed.checked = true;
    embedMedia.checked = false;
    return;
  }
  if (provider === 'gemini') {
    chat.checked = true;
    embed.checked = true;
    embedMedia.checked = true;
    return;
  }
  if (provider === 'openai') {
    chat.checked = true;
    embed.checked = true;
    embedMedia.checked = false;
    return;
  }
  // groq, deepseek — chat only
  chat.checked = true;
  embed.checked = false;
  embedMedia.checked = false;
}

export async function loadTokens() {
  const box = document.getElementById('tokens-list');
  const r = await apiFetch('/api/admin/tokens');
  if (!r.ok) { box.textContent = 'Ошибка загрузки'; return; }
  _tokens = await r.json();
  if (!_tokens.length) {
    box.innerHTML = '<p style=\"color:#475569;font-size:0.85rem\">Нет токенов. Добавьте первый ниже.</p>';
    return;
  }
  box.innerHTML = _tokens.map(t => {
    const hasLimit = t.daily_limit && t.daily_limit > 0;
    const pct2 = hasLimit ? Math.round(t.requests_today / t.daily_limit * 100) : 0;
    const pctClamped = Math.min(Math.max(pct2, 0), 100);
    const barColor = !hasLimit ? '#64748b' : pct2 >= 90 ? '#ef4444' : pct2 >= 70 ? '#f59e0b' : '#22c55e';
    const statusColor = t.status === 'cooldown' ? '#f59e0b' : t.status === 'daily_limit' ? '#ef4444' : t.is_active ? '#22c55e' : '#475569';
    const statusText = t.status === 'cooldown' ? 'cooldown' : t.status === 'daily_limit' ? 'лимит/сутки' : t.is_active ? 'active' : 'inactive';
    const limitText = hasLimit ? `${t.requests_today} / ${t.daily_limit}` : `${t.requests_today} / —`;
    const capsText = (t.capabilities && t.capabilities.length) ? t.capabilities.join(',') : '—';
    const hasChat = t.capabilities && t.capabilities.includes('chat');
    const hasEmbed = t.capabilities && t.capabilities.includes('embed');
    const hasEmbedMedia = t.capabilities && t.capabilities.includes('embed_media');
    return `
    <div class=\"token-row\" id=\"tr-${t.id}\">
      <span class=\"status-dot status-${t.status === 'daily_limit' ? 'inactive' : t.status}\"></span>
      <span class=\"token-badge\">${t.masked}</span>
      <span class=\"token-label\">${t.label || '—'} <span style=\"color:#334155\">(${t.provider})</span> <span style=\"color:#64748b\">[${capsText}]</span></span>
      <div style=\"display:flex;flex-direction:column;gap:3px;min-width:120px\">
        <div style=\"display:flex;justify-content:space-between;font-size:0.7rem;color:#64748b\">
          <span style=\"color:${statusColor}\">${statusText}</span>
          <span>${limitText}</span>
        </div>
        <div style=\"background:#0f1117;border-radius:3px;height:4px\">
          <div style=\"background:${barColor};height:4px;border-radius:3px;width:${pctClamped}%;transition:width .4s\"></div>
        </div>
      </div>
      <button class=\"btn btn-sm btn-ghost\" onclick=\"toggleTokenCapsEditor(${t.id})\">Права</button>
      <button class=\"btn btn-sm btn-ghost\" onclick=\"toggleToken(${t.id})\">${t.is_active ? 'Выкл' : 'Вкл'}</button>
      <button class=\"btn btn-sm btn-danger\" onclick=\"deleteToken(${t.id})\">✕</button>
    </div>
    <div id=\"caps-${t.id}\" style=\"display:none;margin:6px 0 10px 26px;background:#0f1117;border-radius:10px;padding:10px\">
      <div style=\"display:flex;gap:14px;align-items:center;color:#94a3b8;font-size:0.85rem;flex-wrap:wrap\">
        <label style=\"display:flex;gap:6px;align-items:center\">
          <input type=\"checkbox\" id=\"caprow-chat-${t.id}\" ${hasChat ? 'checked' : ''}>
          <span>chat</span>
        </label>
        <label style=\"display:flex;gap:6px;align-items:center\">
          <input type=\"checkbox\" id=\"caprow-embed-${t.id}\" ${hasEmbed ? 'checked' : ''}>
          <span>embed</span>
        </label>
        <label style=\"display:flex;gap:6px;align-items:center\">
          <input type=\"checkbox\" id=\"caprow-embed-media-${t.id}\" ${hasEmbedMedia ? 'checked' : ''}>
          <span>embed_media</span>
        </label>
        <button class=\"btn btn-sm\" onclick=\"saveTokenCaps(${t.id})\">Сохранить</button>
        <button class=\"btn btn-sm btn-ghost\" onclick=\"toggleTokenCapsEditor(${t.id})\">Закрыть</button>
      </div>
    </div>`;
  }).join('');
}

export function toggleTokenCapsEditor(id) {
  const el = document.getElementById(`caps-${id}`);
  if (!el) return;
  const visible = el.style.display !== 'none';
  el.style.display = visible ? 'none' : 'block';
}

export async function saveTokenCaps(id) {
  const chat = document.getElementById(`caprow-chat-${id}`);
  const embed = document.getElementById(`caprow-embed-${id}`);
  const embedMedia = document.getElementById(`caprow-embed-media-${id}`);
  const caps = [];
  if (chat && chat.checked) caps.push('chat');
  if (embed && embed.checked) caps.push('embed');
  if (embedMedia && embedMedia.checked) caps.push('embed_media');
  if (!caps.length) { alert('Выбери хотя бы одну capability'); return; }
  const r = await apiFetch(`/api/admin/tokens/${id}/capabilities`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ capabilities: caps }),
  });
  if (!r.ok) { alert('Ошибка: ' + await r.text()); return; }
  loadTokens();
}

export async function addToken() {
  const token = document.getElementById('inp-token').value.trim();
  const label = document.getElementById('inp-token-label').value.trim();
  const provider = document.getElementById('inp-token-provider').value;
  const caps = [];
  if (document.getElementById('cap-chat').checked) caps.push('chat');
  if (document.getElementById('cap-embed').checked) caps.push('embed');
  if (document.getElementById('cap-embed-media').checked) caps.push('embed_media');
  if (!caps.length) { alert('Выбери хотя бы одну capability'); return; }
  if (!token) { alert('Введите токен'); return; }
  const r = await apiFetch('/api/admin/tokens', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, label, provider, capabilities: caps }),
  });
  if (!r.ok) { alert('Ошибка: ' + await r.text()); return; }
  document.getElementById('inp-token').value = '';
  document.getElementById('inp-token-label').value = '';
  loadTokens();
}

export async function deleteToken(id) {
  if (!confirm('Удалить токен?')) return;
  await apiFetch(`/api/admin/tokens/${id}`, { method: 'DELETE' });
  loadTokens();
}

export async function toggleToken(id) {
  await apiFetch(`/api/admin/tokens/${id}/toggle`, { method: 'PATCH' });
  loadTokens();
}
