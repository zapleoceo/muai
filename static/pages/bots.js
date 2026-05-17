const v = window.APP_VERSION ? `?v=${window.APP_VERSION}` : '';

let _apiFetch = null;

export function initBotsPage(apiFetch) {
  _apiFetch = apiFetch;
}

function isOnline(lastSeenAt) {
  if (!lastSeenAt) return false;
  return (Date.now() - new Date(lastSeenAt).getTime()) < 90_000;
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export async function loadBots() {
  const el = document.getElementById('bots-list');
  if (!el) return;
  el.textContent = 'Загрузка…';
  const r = await _apiFetch('/api/admin/executor/bots');
  if (!r.ok) { el.textContent = 'Ошибка загрузки'; return; }
  const bots = await r.json();
  if (!bots.length) { el.innerHTML = '<p style="color:#64748b">Нет зарегистрированных ботов</p>'; return; }

  el.innerHTML = bots.map(bot => {
    const online = isOnline(bot.last_seen_at);
    const statusDot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${online ? '#22c55e' : '#ef4444'};margin-right:6px"></span>`;
    const chatsHtml = bot.chats.length
      ? bot.chats.map(c => `<span class="bl-tag" style="font-size:0.72rem">${c.chat_title || c.chat_id}</span>`).join('')
      : '<span style="color:#475569;font-size:0.78rem">нет чатов</span>';

    return `
      <div class="section" style="margin-bottom:12px;padding:14px">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <div style="font-weight:600;font-size:1rem">${statusDot}${bot.name}</div>
          <div style="color:#94a3b8;font-size:0.8rem">@${bot.bot_username || '?'}</div>
          <div style="color:#64748b;font-size:0.75rem">id=${bot.id}</div>
          <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
            <label style="font-size:0.8rem;color:#94a3b8">Режим:</label>
            <select class="inp" style="font-size:0.8rem;padding:4px 8px" onchange="saveBotMode(${bot.id}, this.value)">
              <option value="mentions" ${bot.forward_mode === 'mentions' ? 'selected' : ''}>только упоминания</option>
              <option value="replies" ${bot.forward_mode === 'replies' ? 'selected' : ''}>упоминания + ответы</option>
              <option value="all" ${bot.forward_mode === 'all' ? 'selected' : ''}>все сообщения</option>
            </select>
            <label class="cap-label" style="margin-left:4px">
              <input type="checkbox" ${bot.is_active ? 'checked' : ''} onchange="saveBotEnabled(${bot.id}, this.checked)">
              <span style="font-size:0.8rem">Активен</span>
            </label>
          </div>
        </div>
        <div style="margin-top:6px;color:#64748b;font-size:0.75rem">
          Последний пинг: ${fmtDate(bot.last_seen_at)} · API: ${bot.api_url || '—'}
        </div>
        <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:4px">${chatsHtml}</div>
      </div>
    `;
  }).join('');
}

export async function saveBotMode(botId, mode) {
  await _apiFetch(`/api/admin/executor/bots/${botId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ forward_mode: mode }),
  });
}

export async function saveBotEnabled(botId, enabled) {
  await _apiFetch(`/api/admin/executor/bots/${botId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_enabled: enabled }),
  });
}

const STATUS_LABELS = { pending: '⏳ ждёт', notified: '🔔 уведомлено', replied: '✅ отправлено', ignored: '❌ проигнорировано' };

export async function loadInbox() {
  const el = document.getElementById('inbox-list');
  if (!el) return;
  el.textContent = 'Загрузка…';
  const filter = document.getElementById('inbox-filter')?.value || '';
  const r = await _apiFetch('/api/admin/executor/inbox?limit=30');
  if (!r.ok) { el.textContent = 'Ошибка загрузки'; return; }
  let items = await r.json();
  if (filter) items = items.filter(i => i.status === filter);
  if (!items.length) { el.innerHTML = '<p style="color:#64748b">Нет записей</p>'; return; }

  el.innerHTML = `<table class="chat-table" style="font-size:0.82rem">
    <thead><tr>
      <th>Чат</th><th>От</th><th>Сообщение</th><th>Цитата</th><th>Статус</th><th>Время</th>
    </tr></thead>
    <tbody>
    ${items.map(i => `<tr>
      <td>${i.chat_title || i.chat_id}</td>
      <td>${i.from_user_name || '?'}</td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(i.text || '').replace(/"/g, '&quot;')}">${i.text || ''}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#94a3b8" title="${(i.quoted_text || '').replace(/"/g, '&quot;')}">${i.quoted_from ? `${i.quoted_from}: ` : ''}${i.quoted_text || '—'}</td>
      <td>${STATUS_LABELS[i.status] || i.status}</td>
      <td style="white-space:nowrap">${fmtDate(i.created_at)}</td>
    </tr>`).join('')}
    </tbody>
  </table>`;
}
