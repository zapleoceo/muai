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

  el.innerHTML = bots.map(bot => renderBotCard(bot)).join('') +
    `<div style="margin-top:16px">${renderAddForm()}</div>`;
}

function renderBotCard(bot) {
  const online = isOnline(bot.last_seen_at);
  const running = bot.is_running;

  const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;
    background:${running ? '#22c55e' : (bot.is_active ? '#f59e0b' : '#ef4444')};
    margin-right:6px;flex-shrink:0" title="${running ? 'Работает' : (bot.is_active ? 'Активен, не запущен' : 'Отключён')}"></span>`;

  const badge = running
    ? `<span style="font-size:0.7rem;background:#14532d;color:#86efac;padding:2px 7px;border-radius:10px">running</span>`
    : `<span style="font-size:0.7rem;background:#1f2937;color:#6b7280;padding:2px 7px;border-radius:10px">stopped</span>`;

  const chatsHtml = bot.chats.length
    ? bot.chats.map(c =>
        `<span style="font-size:0.72rem;background:#1e293b;color:#94a3b8;padding:2px 8px;border-radius:10px">${c.chat_title || c.chat_id}</span>`
      ).join('')
    : `<span style="color:#475569;font-size:0.78rem">нет чатов</span>`;

  return `
    <div style="background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:10px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        ${dot}
        <span style="font-weight:600">${bot.name}</span>
        <span style="color:#64748b;font-size:0.82rem">@${bot.bot_username || '?'}</span>
        ${badge}
        <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
          <select class="inp" style="font-size:0.78rem;padding:3px 8px"
            onchange="saveBotMode(${bot.id}, this.value)">
            <option value="mentions" ${bot.forward_mode === 'mentions' ? 'selected' : ''}>только упоминания</option>
            <option value="replies"  ${bot.forward_mode === 'replies'  ? 'selected' : ''}>упоминания + ответы</option>
            <option value="all"      ${bot.forward_mode === 'all'      ? 'selected' : ''}>все сообщения</option>
          </select>
          <label style="display:flex;align-items:center;gap:4px;font-size:0.8rem;cursor:pointer">
            <input type="checkbox" ${bot.is_active ? 'checked' : ''}
              onchange="saveBotEnabled(${bot.id}, this.checked)">
            Активен
          </label>
          <button class="btn btn-sm" style="background:#7f1d1d;color:#fca5a5;padding:3px 10px;font-size:0.78rem"
            onclick="deleteBot(${bot.id}, '${(bot.name || '').replace(/'/g, "\\'")}')">Удалить</button>
        </div>
      </div>
      <div style="margin-top:6px;color:#475569;font-size:0.72rem">
        Пинг: ${fmtDate(bot.last_seen_at)} · ${bot.api_url ? `HTTP: ${bot.api_url}` : 'DB-управляемый'}
      </div>
      <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">${chatsHtml}</div>
    </div>`;
}

function renderAddForm() {
  return `
    <div style="background:#1e293b;border-radius:10px;padding:16px">
      <h3 style="margin:0 0 12px;font-size:0.95rem;color:#94a3b8">Добавить бота</h3>
      <div style="display:flex;flex-direction:column;gap:8px;max-width:520px">
        <input class="inp" id="new-bot-token" type="password" placeholder="Bot Token (от @BotFather)" autocomplete="off">
        <input class="inp" id="new-bot-name" type="text" placeholder="Название (необязательно)">
        <select class="inp" id="new-bot-mode">
          <option value="mentions">только упоминания</option>
          <option value="replies">упоминания + ответы</option>
          <option value="all">все сообщения</option>
        </select>
        <div id="new-bot-error" style="color:#f87171;font-size:0.82rem;display:none"></div>
        <button class="btn btn-sm" onclick="createBot(this)" style="width:fit-content">
          ➕ Добавить бота
        </button>
      </div>
    </div>`;
}

export async function createBot(btn) {
  const token = document.getElementById('new-bot-token')?.value.trim();
  const name  = document.getElementById('new-bot-name')?.value.trim();
  const mode  = document.getElementById('new-bot-mode')?.value;
  const errEl = document.getElementById('new-bot-error');

  if (!token) { errEl.textContent = 'Введите токен'; errEl.style.display = ''; return; }
  errEl.style.display = 'none';

  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Проверяю токен…';

  const r = await _apiFetch('/api/admin/executor/bots', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bot_token: token, name, forward_mode: mode }),
  });

  btn.disabled = false;
  btn.textContent = orig;

  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    errEl.textContent = data.detail || 'Ошибка создания бота';
    errEl.style.display = '';
    return;
  }

  const data = await r.json();
  alert(`Бот @${data.bot_username} добавлен и запущен!`);
  loadBots();
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
  setTimeout(loadBots, 800);
}

export async function deleteBot(botId, name) {
  if (!confirm(`Удалить бота «${name}»?\nИстория входящих сохранится.`)) return;
  const r = await _apiFetch(`/api/admin/executor/bots/${botId}`, { method: 'DELETE' });
  if (r.ok) loadBots();
  else alert('Ошибка удаления');
}

const STATUS_LABELS = {
  pending:  '⏳ ждёт',
  notified: '🔔 уведомлено',
  replied:  '✅ отправлено',
  ignored:  '❌ проигнорировано',
};

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
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${(i.text || '').replace(/"/g, '&quot;')}">${i.text || ''}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#94a3b8"
          title="${(i.quoted_text || '').replace(/"/g, '&quot;')}">${i.quoted_from ? `${i.quoted_from}: ` : ''}${i.quoted_text || '—'}</td>
      <td>${STATUS_LABELS[i.status] || i.status}</td>
      <td style="white-space:nowrap">${fmtDate(i.created_at)}</td>
    </tr>`).join('')}
    </tbody>
  </table>`;
}
