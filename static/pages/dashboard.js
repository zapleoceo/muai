import { apiFetch, esc, fmt, pct } from '../api.js';

let _stream = null;
let _streamRetry = null;
let _streamBackoffMs = 2000;

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

function _updateMediaEmbedderBtn(running) {
  const btn = document.getElementById('media-embedder-toggle-btn');
  if (!btn) return;
  if (running) {
    btn.textContent = '■';
    btn.title = 'Остановить эмбеддинг файлов';
    btn.dataset.running = '1';
    btn.style.color = '#ef4444';
  } else {
    btn.textContent = '▶';
    btn.title = 'Запустить эмбеддинг файлов';
    btn.dataset.running = '0';
    btn.style.color = '';
  }
}

function _isDashboardActive() {
  const tab = document.getElementById('tab-dashboard');
  return Boolean(tab && tab.classList.contains('active'));
}

function _renderStats(d) {
  const t = d.totals;
  document.getElementById('totals').innerHTML = `
    <div class="card">
      <div class="label">Сообщений</div>
      <div class="value">${fmt(t.messages)}</div>
      <div class="sub">Вход: ${fmt(t.incoming)} (${pct(t.incoming, t.messages)}%)</div>
      <div class="sub">Исх: ${fmt(t.outgoing)} (${pct(t.outgoing, t.messages)}%)</div>
    </div>
    <div class="card">
      <div class="label">Чаты</div>
      <div class="value">${fmt(t.chats)}</div>
      <div class="label" style="margin-top:10px">Пользователи</div>
      <div class="value">${fmt(t.users)}</div>
    </div>
    <div class="card"><div class="label">Размер БД</div><div class="value" style="font-size:1.3rem">${t.db_size || '—'}</div><div class="sub">сообщения: ${t.messages_size || '—'}</div></div>
    <div class="card">
      <div class="label">Чанки (таблица)</div>
      <div class="mini-table">
        <div></div><div class="mt-h">Чанки</div><div class="mt-h">Чаты</div>
        <div class="mt-k">Текст чатов</div><div class="mt-v">${fmt(t.chunks || 0)}</div><div class="mt-v">${fmt(t.embedded_chats || 0)}</div>
        <div class="mt-k">Файлов</div><div class="mt-v">${fmt(t.media_chunks || 0)}</div><div class="mt-v">${fmt(t.media_embedded_chats || 0)}</div>
      </div>
    </div>
    <div class="card">
      <div class="label">Сообщений за 7 дней</div>
      <div class="day-chart" id="daily-chart"></div>
    </div>
  `;

  const daily = d.daily;
  const daily7 = (daily || []).slice(-7);
  const maxD = Math.max(...daily7.map(x => x.count), 1);
  const MAX_BAR_PX = 34;
  const MIN_BAR_PX = 3;
  document.getElementById('daily-chart').innerHTML = daily7.map(x => {
    const day = String(x.day ?? '').split('-').pop() || '';
    const h = Math.round(x.count / maxD * MAX_BAR_PX);
    const barPx = Math.max(MIN_BAR_PX, h);
    return `
      <div class="day-col" title="${x.count}">
        <div class="day-bar" style="height:${barPx}px"></div>
        <div class="day-label">${day}</div>
      </div>
    `;
  }).join('');

  const maxC = Math.max(...d.top_chats.map(x => x.count), 1);
  document.getElementById('top-chats').innerHTML = d.top_chats.map(x => `
    <div class="bar-row">
      <div class="name" title="${x.title}">${x.title}</div>
      <div class="bar-wrap"><div class="bar" style="width:${Math.round(x.count / maxC * 100)}%"></div></div>
      <div class="bar-count">${x.count}</div>
    </div>
  `).join('');
}

function _renderEmbedder(d) {
  const box = document.getElementById('embedder-status');
  if (!box) return;
  _updateEmbedderBtn(d.running);
  if (d.chat_types) _syncTextTypeCheckboxes(d.chat_types);
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

function _renderMediaEmbedder(d) {
  const box = document.getElementById('media-embedder-status');
  if (!box) return;
  _updateMediaEmbedderBtn(d.running);
  const runBadge = d.running
    ? `<span class="badge badge-active">⚙️ работает</span> <b>${esc(d.current_item || '')}</b>`
    : `<span class="badge badge-disabled">⏸ ожидание</span>`;
  const lastRunHtml = d.last_run
    ? `<div>Последний запуск: <b>${new Date(d.last_run).toLocaleString('ru')}</b></div>`
    : '';
  const errHtml = d.last_errors?.length
    ? `<div style="margin-top:8px;color:#ef4444;font-size:.85rem">Последние ошибки:<br>${d.last_errors.map(e => `• ${esc(e)}`).join('<br>')}</div>`
    : '';
  box.innerHTML = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <div>${runBadge}</div>
      <div>Обработано: <b>${fmt(d.items_done || 0)}</b></div>
      <div>Добавлено чанков: <b>${fmt(d.chunks_added || 0)}</b></div>
      <div>Всего: <b>${fmt(d.total_chunks || 0)}</b></div>
      <div>Ожидают: <b style="color:${d.pending > 0 ? '#fbbf24' : '#4ade80'}">${fmt(d.pending ?? '—')}</b></div>
      ${lastRunHtml}
    </div>
    ${errHtml}`;
}

function _stopStream() {
  if (_streamRetry) {
    clearTimeout(_streamRetry);
    _streamRetry = null;
  }
  if (_stream) {
    try { _stream.close(); } catch (e) {}
    _stream = null;
  }
}

function _startStreamIfNeeded() {
  if (_stream || _streamRetry) return;
  if (!_isDashboardActive()) return;
  if (document.visibilityState !== 'visible') return;
  _stream = new EventSource('/api/admin/stream');
  _stream.onmessage = (ev) => {
    if (!_isDashboardActive()) return;
    try {
      const payload = JSON.parse(ev.data || '{}');
      if (payload.stats) _renderStats(payload.stats);
      if (payload.embedder) _renderEmbedder(payload.embedder);
      if (payload.media_embedder) _renderMediaEmbedder(payload.media_embedder);
    } catch (e) {}
  };
  _stream.onerror = () => {
    _stopStream();
    if (_isDashboardActive() && document.visibilityState === 'visible') {
      const wait = _streamBackoffMs;
      _streamBackoffMs = Math.min(_streamBackoffMs * 2, 60000);
      _streamRetry = setTimeout(() => {
        _streamRetry = null;
        _startStreamIfNeeded();
      }, wait);
    }
  };
}

export async function loadStats() {
  const r = await apiFetch('/api/admin/stats');
  if (r.status === 401) {
    document.getElementById('login').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
    return;
  }
  const d = await r.json();
  _renderStats(d);
  _streamBackoffMs = 2000;
  _startStreamIfNeeded();
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
  if (running) {
    await apiFetch('/api/admin/embedder/stop', { method: 'POST' });
  } else {
    const types = _selectedTextTypes();
    if (!types.length) { alert('Выбери хотя бы один тип чата'); return; }
    await apiFetch('/api/admin/embedder/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_types: types }),
    });
  }
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
  _renderEmbedder(d);
  _streamBackoffMs = 2000;
  _startStreamIfNeeded();
}

function _selectedMediaTypes() {
  const box = document.getElementById('media-embedder-types');
  if (!box) return [];
  return Array.from(box.querySelectorAll('input[type=checkbox]'))
    .filter(x => x.checked)
    .map(x => x.value);
}

function _selectedTextTypes() {
  const box = document.getElementById('text-embedder-types');
  if (!box) return ['private', 'group'];
  return Array.from(box.querySelectorAll('input[type=checkbox]'))
    .filter(x => x.checked)
    .map(x => x.value);
}

function _syncTextTypeCheckboxes(types) {
  const box = document.getElementById('text-embedder-types');
  if (!box) return;
  box.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.checked = types.includes(cb.value);
  });
}

export async function clearMediaChunks() {
  const ok = confirm(
    '⚠️ Очистка базы файловых чанков\n\n' +
    'Будут удалены ВСЕ чанки файлов.\n\n' +
    'Продолжить?'
  );
  if (!ok) return;
  const r = await apiFetch('/api/admin/media-embedder/chunks', { method: 'DELETE' });
  if (r.ok) {
    const d = await r.json();
    await loadMediaEmbedder();
    alert(`Удалено ${d.deleted} файловых чанков.`);
  }
}

export async function toggleMediaEmbedder() {
  const btn = document.getElementById('media-embedder-toggle-btn');
  const running = btn.dataset.running === '1';
  if (running) {
    await apiFetch('/api/admin/media-embedder/stop', { method: 'POST' });
  } else {
    const types = _selectedMediaTypes();
    if (!types.length) {
      alert('Выбери хотя бы один тип файла');
      return;
    }
    await apiFetch('/api/admin/media-embedder/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ types }),
    });
  }
  await loadMediaEmbedder();
}

export async function loadMediaEmbedder() {
  const box = document.getElementById('media-embedder-status');
  if (!box) return;
  const r = await apiFetch('/api/admin/media-embedder/status');
  if (!r.ok) {
    box.innerHTML = '<span style="color:#ef4444">Ошибка загрузки</span>';
    return;
  }
  const d = await r.json();
  _renderMediaEmbedder(d);
  _streamBackoffMs = 2000;
  _startStreamIfNeeded();
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') _startStreamIfNeeded();
  else _stopStream();
});

export async function loadLogs() {
  const b1 = document.getElementById('logs-embedder');
  const b2 = document.getElementById('logs-bot');
  const b3 = document.getElementById('logs-other');
  if (!b1 || !b2 || !b3) return;
  b1.textContent = 'Загрузка…';
  b2.textContent = 'Загрузка…';
  b3.textContent = 'Загрузка…';
  const r = await apiFetch('/api/admin/logs?lines=200&split=1');
  if (!r.ok) {
    const msg = 'Ошибка загрузки логов';
    b1.textContent = msg;
    b2.textContent = msg;
    b3.textContent = msg;
    return;
  }
  const d = await r.json();
  b1.textContent = d.embedder || '';
  b2.textContent = d.bot || '';
  b3.textContent = d.other || '';
  b1.scrollTop = b1.scrollHeight;
  b2.scrollTop = b2.scrollHeight;
  b3.scrollTop = b3.scrollHeight;
}
