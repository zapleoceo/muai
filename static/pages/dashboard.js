import { apiFetch, esc, fmt, pct } from '../api.js';

function _updateEmbedderBtn(running) {
  const btn = document.getElementById('embedder-toggle-btn');
  if (!btn) return;
  if (running) {
    btn.textContent = '■';
    btn.title = 'Остановить чанкование';
    btn.dataset.running = '1';
    btn.style.color = '#ef4444';
  } else {
    btn.textContent = '▶';
    btn.title = 'Запустить чанкование';
    btn.dataset.running = '0';
    btn.style.color = '';
  }
}

export async function loadStats() {
  const r = await apiFetch('/api/admin/stats');
  if (r.status === 401) {
    document.getElementById('login').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
    return;
  }
  const d = await r.json();

  const t = d.totals;
  document.getElementById('totals').innerHTML = `
    <div class="card">
      <div class="label">Сообщений</div>
      <div class="value">${fmt(t.messages)}</div>
      <div class="sub">Вход: ${fmt(t.incoming)} (${pct(t.incoming, t.messages)}%)</div>
      <div class="sub">Исх: ${fmt(t.outgoing)} (${pct(t.outgoing, t.messages)}%)</div>
    </div>
    <div class="card">
      <div class="label">Чаты / Пользователи</div>
      <div class="value">${fmt(t.chats)} / ${fmt(t.users)}</div>
    </div>
    <div class="card"><div class="label">Размер БД</div><div class="value" style="font-size:1.3rem">${t.db_size || '—'}</div><div class="sub">сообщения: ${t.messages_size || '—'}</div></div>
    <div class="card"><div class="label">Чанков (мозг)</div><div class="value">${fmt(t.chunks || 0)}</div><div class="sub">в ${fmt(t.embedded_chats || 0)} чатах</div></div>
    <div class="card">
      <div class="label">Сообщений за 7 дней</div>
      <div class="day-chart" id="daily-chart"></div>
    </div>
  `;

  const daily = d.daily;
  const maxD = Math.max(...daily.map(x => x.count), 1);
  document.getElementById('daily-chart').innerHTML = daily.map(x => `
    <div class="day-col">
      <div class="day-bar" style="height:${Math.round(x.count / maxD * 70) + 2}px" title="${x.count}"></div>
      <div class="day-label">${x.day.slice(5)}</div>
    </div>
  `).join('');

  const maxC = Math.max(...d.top_chats.map(x => x.count), 1);
  document.getElementById('top-chats').innerHTML = d.top_chats.map(x => `
    <div class="bar-row">
      <div class="name" title="${x.title}">${x.title}</div>
      <div class="bar-wrap"><div class="bar" style="width:${Math.round(x.count / maxC * 100)}%"></div></div>
      <div class="bar-count">${x.count}</div>
    </div>
  `).join('');
}

export async function clearChunks() {
  const ok = confirm(
    '⚠️ Очистка векторной базы\n\n' +
    'Будут удалены ВСЕ чанки — весь «мозг» бота.\n' +
    'Бот перестанет находить контекст из истории чатов до окончания повторного чанкования.\n\n' +
    'Повторное чанкование запускается вручную кнопкой ▶ и займёт длительное время.\n\n' +
    'Продолжить?'
  );
  if (!ok) return;
  const r = await apiFetch('/api/admin/embedder/chunks', { method: 'DELETE' });
  if (r.ok) {
    const d = await r.json();
    await loadEmbedder();
    alert(`Удалено ${d.deleted} чанков. Запусти эмбеддер кнопкой ▶ для повторного чанкования.`);
  }
}

export async function toggleEmbedder() {
  const btn = document.getElementById('embedder-toggle-btn');
  const running = btn.dataset.running === '1';
  await apiFetch(`/api/admin/embedder/${running ? 'stop' : 'restart'}`, { method: 'POST' });
  await loadEmbedder();
}

export async function loadEmbedder() {
  const box = document.getElementById('embedder-status');
  const r = await apiFetch('/api/admin/embedder/status');
  if (!r.ok) {
    box.innerHTML = '<span style="color:#ef4444">Ошибка загрузки</span>';
    return;
  }
  const d = await r.json();
  _updateEmbedderBtn(d.running);
  const runBadge = d.running
    ? `<span class="badge badge-active">⚙️ работает</span> чат: <b>${esc(d.current_chat)}</b>`
    : `<span class="badge badge-disabled">⏸ ожидание</span>`;
  const lastRunHtml = d.last_run
    ? `<div>Последний запуск: <b>${new Date(d.last_run).toLocaleString('ru')}</b></div>`
    : '';
  const errHtml = d.last_errors?.length
    ? `<div style="margin-top:8px;color:#ef4444;font-size:.85rem">Последние ошибки:<br>${d.last_errors.map(e => `• ${esc(e)}`).join('<br>')}</div>`
    : '';
  const queue = d.pending_by_chat || [];
  const maxQ = Math.max(...queue.map(x => x.pending), 1);
  const queueHtml = queue.length
    ? `<div style="margin-top:6px">${queue.slice(0, 12).map(c => `
        <div class="bar-row">
          <div class="name" title="${esc(c.title || c.chat_id)}">${esc(c.title || String(c.chat_id))}</div>
          <div class="bar-wrap"><div class="bar" style="width:${Math.round(c.pending / maxQ * 100)}%"></div></div>
          <div class="bar-count">${fmt(c.pending)}</div>
        </div>`).join('')}</div>`
    : `<div style="margin-top:6px;color:#64748b">Очередь пуста</div>`;
  box.innerHTML = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <div>${runBadge}</div>
      <div>Чатов обработано: <b>${d.chats_done}</b></div>
      <div>Добавлено чанков: <b>${d.chunks_added}</b></div>
      <div>Всего в мозге: <b>${fmt(d.total_chunks)}</b></div>
      <div>Ожидают чанкования: <b style="color:${d.messages_pending > 0 ? '#fbbf24' : '#4ade80'}">${fmt(d.messages_pending ?? '—')}</b></div>
      ${lastRunHtml}
    </div>
    <div style="font-size:.9rem;color:#94a3b8">Очередь чанкования</div>
    ${queueHtml}
    ${errHtml}`;
}

export async function loadLogs() {
  const box = document.getElementById('logs-box');
  box.textContent = 'Загрузка…';
  const r = await apiFetch('/api/admin/logs?lines=150');
  if (!r.ok) {
    box.textContent = 'Ошибка загрузки логов';
    return;
  }
  const d = await r.json();
  box.textContent = d.logs;
  box.scrollTop = box.scrollHeight;
}
