import { apiFetch, esc, fmt } from '../api.js';

const PAGE_SIZE = 50;
let _allChats = [];
let _filter = 'all';
let _search = '';
let _folderFilter = '';
let _sortCol = 'title';
let _sortDir = 1;
let _page = 0;
let _syncPollTimer = null;

export function initChatsPage() {
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-action]');
    if (!btn || !btn.closest('#chat-table-wrap')) return;
    const action = btn.dataset.action;
    const id = parseInt(btn.dataset.id);
    if (!id) return;

    if (action === 'approve') {
      const depthStr = prompt('Глубина синхронизации (дней, пусто = глобальная):');
      if (depthStr === null) return;
      const depth = depthStr.trim() ? parseInt(depthStr) : null;
      await apiFetch(`/api/admin/chats/${id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depth_days: depth }),
      });
      await loadChats();
    } else if (action === 'skip') {
      const reason = prompt('Причина отключения (необязательно):') ?? '';
      await apiFetch(`/api/admin/chats/${id}/skip`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      await loadChats();
    } else if (action === 'toggle-topics') {
      const expanded = btn.textContent.trim() === '▼';
      btn.textContent = expanded ? '▶' : '▼';
      document.querySelectorAll(`tr.topic-row[data-parent=\"${id}\"]`).forEach(r => {
        r.style.display = expanded ? 'none' : '';
      });
      return;
    } else if (action === 'sync-now') {
      await apiFetch(`/api/admin/chats/${id}/sync-now`, { method: 'POST' });
    } else if (action === 'cancel') {
      await apiFetch(`/api/admin/chats/${id}/cancel-sync`, { method: 'POST' });
    } else if (action === 'delete') {
      const title = btn.dataset.title || String(id);
      if (!confirm(`Удалить все сообщения чата «${title}»?`)) return;
      const r = await apiFetch(`/api/admin/chats/${id}/messages`, { method: 'DELETE' });
      if (r.ok) {
        const d = await r.json();
        alert(`Удалено ${d.deleted} сообщений`);
      } else {
        alert('Ошибка удаления: ' + r.status);
      }
      await loadChats();
    }
  });

  const pg = document.getElementById('chat-pagination');
  if (pg) {
    pg.addEventListener('click', e => {
      const btn = e.target.closest('button');
      if (!btn) return;
      const nav = btn.dataset.nav;
      const page = btn.dataset.page;
      if (nav === 'prev') _page = Math.max(0, _page - 1);
      else if (nav === 'next') _page = Math.min(parseInt(btn.dataset.max), _page + 1);
      else if (page) _page = parseInt(page);
      else return;
      renderChats();
    });
  }

  document.addEventListener('click', async e => {
    const cell = e.target.closest('.depth-cell');
    if (!cell || cell.querySelector('input')) return;

    const chatId = parseInt(cell.dataset.id);
    const curDepth = cell.dataset.depth;

    const input = document.createElement('input');
    input.type = 'number';
    input.min = '1';
    input.max = '3650';
    input.className = 'depth-input';
    input.value = curDepth || '';
    input.placeholder = 'глоб.';
    cell.textContent = '';
    cell.appendChild(input);
    input.focus();
    input.select();

    async function saveDepth() {
      const val = input.value.trim();
      const depth = val ? parseInt(val) : null;
      const r = await apiFetch(`/api/admin/chats/${chatId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depth_days: depth }),
      });
      if (r.ok) {
        cell.dataset.depth = depth ?? '';
        cell.textContent = depth ? `${depth}д` : 'глоб.';
      } else {
        cell.textContent = curDepth ? `${curDepth}д` : 'глоб.';
      }
    }

    input.addEventListener('blur', saveDepth);
    input.addEventListener('keydown', e2 => {
      if (e2.key === 'Enter') input.blur();
      if (e2.key === 'Escape') {
        input.removeEventListener('blur', saveDepth);
        cell.textContent = curDepth ? `${curDepth}д` : 'глоб.';
      }
    });
  });
}

export async function loadChats() {
  const r = await apiFetch('/api/admin/chats');
  if (!r.ok) return;
  _allChats = await r.json();
  _page = 0;
  updateFolderDropdown();
  renderChats();
}

function updateFolderDropdown() {
  const sel = document.getElementById('folder-filter');
  const cur = sel.value;
  const folders = [...new Set(_allChats.map(c => c.folder).filter(Boolean))].sort();
  sel.innerHTML = '<option value=\"\">Все папки</option>' +
    folders.map(f => `<option value=\"${esc(f)}\" ${f === cur ? 'selected' : ''}>${esc(f)}</option>`).join('');
}

export async function syncFolders() {
  const btn = document.querySelector('[onclick=\"syncFolders()\"]');
  if (btn) btn.textContent = '⏳ Папки';
  const r = await apiFetch('/api/admin/chats/sync-folders', { method: 'POST' });
  if (btn) btn.textContent = '↻ Папки';
  if (r.ok) {
    const d = await r.json();
    await loadChats();
    alert(`Обновлено ${d.updated} чатов`);
  }
}

export function showAvatar(src, name) {
  const box = document.createElement('div');
  box.className = 'lightbox';
  box.innerHTML = `<img src=\"${src}\"><div class=\"lightbox-name\">${name}</div>`;
  box.onclick = () => box.remove();
  document.body.appendChild(box);
}

export async function syncTopics() {
  const btn = document.getElementById('sync-topics-btn');
  if (btn) btn.textContent = '⏳ Ветки';
  const r = await apiFetch('/api/admin/chats/sync-topics', { method: 'POST' });
  if (btn) btn.textContent = '↻ Ветки';
  if (r.ok) {
    const d = await r.json();
    await loadChats();
    alert(`Обновлено ${d.updated} форум-групп`);
  }
}

export function onSearch(val) {
  _search = val.trim().toLowerCase();
  _page = 0;
  renderChats();
}

export function setFilter(f, el) {
  _filter = f;
  _page = 0;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  renderChats();
}

export function sortBy(col) {
  if (_sortCol === col) _sortDir *= -1;
  else { _sortCol = col; _sortDir = 1; }
  _page = 0;
  updateSortHeaders();
  renderChats();
}

export function onFolderFilter(val) {
  _folderFilter = val;
  _page = 0;
  renderChats();
}

function updateSortHeaders() {
  const cols = ['type', 'title', 'folder', 'status', 'message_count', 'depth_days', 'last_synced_at'];
  const thId = { 'message_count': 'msgs', 'depth_days': 'depth', 'last_synced_at': 'synced' };
  const arrows = { 1: '↑', '-1': '↓' };
  cols.forEach(c => {
    const th = document.getElementById('th-' + (thId[c] || c));
    if (!th) return;
    th.classList.toggle('sorted', _sortCol === c);
    th.querySelector('.sort-arrow').textContent = _sortCol === c ? arrows[String(_sortDir)] : '↕';
  });
}

function getVisible() {
  let chats = _allChats;
  if (_filter !== 'all') chats = chats.filter(c => c.status === _filter);
  if (_folderFilter) chats = chats.filter(c => (c.folder || '') === _folderFilter);
  if (_search) {
    chats = chats.filter(c =>
      (c.title || '').toLowerCase().includes(_search) ||
      (c.username || '').toLowerCase().includes(_search) ||
      (c.folder || '').toLowerCase().includes(_search) ||
      String(c.id).includes(_search)
    );
  }
  chats = [...chats].sort((a, b) => {
    let av = a[_sortCol] ?? '';
    let bv = b[_sortCol] ?? '';
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return -_sortDir;
    if (av > bv) return _sortDir;
    return 0;
  });
  return chats;
}

export function renderChats() {
  const tbody = document.getElementById('chat-list');
  const chats = getVisible();
  const total = chats.length;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (_page >= pageCount) _page = pageCount - 1;
  const slice = chats.slice(_page * PAGE_SIZE, (_page + 1) * PAGE_SIZE);

  const STATUS = { active: 'активен', pending: 'ожидает', disabled: 'отключён', unknown: 'неизвестно' };
  const SC = { active: 'cs-active', pending: 'cs-pending', disabled: 'cs-disabled', unknown: 'cs-unknown' };

  if (!slice.length) {
    tbody.innerHTML = `<tr><td colspan=\"9\" style=\"color:#475569;padding:20px;text-align:center\">Нет чатов</td></tr>`;
  } else {
    const rows = [];
    for (const c of slice) {
      const depth = c.depth_days ? `${c.depth_days}д` : 'глоб.';
      const uname = c.username ? `<span class=\"chat-username\">@${esc(c.username)}</span>` : '';
      const approveBtn = c.status !== 'active'
        ? `<button class=\"btn btn-sm btn-success\" data-action=\"approve\" data-id=\"${c.id}\" title=\"Включить синхронизацию\">✓</button>` : '';
      const disableBtn = c.status === 'active'
        ? `<button class=\"btn btn-sm btn-ghost\" data-action=\"skip\" data-id=\"${c.id}\" title=\"Отключить\">—</button>` : '';
      const cancelBtn = c.status === 'active'
        ? `<button class=\"btn btn-sm btn-ghost\" data-action=\"cancel\" data-id=\"${c.id}\" title=\"Отменить текущую синхр.\">✕</button>` : '';
      const syncNowBtn = c.status === 'active'
        ? `<button class=\"btn btn-sm btn-ghost\" data-action=\"sync-now\" data-id=\"${c.id}\" title=\"Синхронизировать сейчас\">⚡</button>` : '';
      const deleteBtn = `<button class=\"btn btn-sm btn-danger\" data-action=\"delete\" data-id=\"${c.id}\" data-title=\"${esc(c.title)}\" title=\"Удалить сообщения\">🗑</button>`;
      const topics = c.topics || [];
      const topicToggle = topics.length
        ? `<button class=\"topic-toggle\" data-action=\"toggle-topics\" data-id=\"${c.id}\" title=\"${topics.length} веток\">▶</button> ` : '';
      const tgLink = c.username ? `https://t.me/${c.username}` : null;
      const avatarImg = `<img src=\"/api/admin/chats/${c.id}/avatar\" class=\"chat-avatar\" onclick=\"showAvatar(this.src,'${esc(c.title)}')\" onerror=\"this.style.display='none'\">`;
      const titleEl = tgLink
        ? `<a href=\"${tgLink}\" target=\"_blank\" rel=\"noopener\" class=\"chat-title-link\" title=\"${esc(c.title)}\">${esc(c.title)}</a>`
        : `<span class=\"chat-title-text\" title=\"${esc(c.title)}\">${esc(c.title)}</span>`;
      rows.push(`<tr>
        <td class=\"avatar-cell\">${avatarImg}</td>
        <td><span class=\"chat-type-badge\">${esc(c.type)}</span></td>
        <td class=\"chat-name-cell\">${topicToggle}${titleEl}${uname}</td>
        <td style=\"color:#64748b;font-size:0.78rem\">${c.folder ? esc(c.folder) : '<span style=\"color:#334155\">—</span>'}</td>
        <td><span class=\"chat-status-badge ${SC[c.status] || 'cs-unknown'}\">${STATUS[c.status] || c.status}</span></td>
        <td style=\"text-align:right;color:#94a3b8\">${fmt(c.message_count)}</td>
        <td style=\"color:#475569;font-size:0.78rem\">
          <span class=\"depth-cell\" data-id=\"${c.id}\" data-depth=\"${c.depth_days ?? ''}\" title=\"Нажмите для изменения глубины\">${depth}</span>
        </td>
        <td style=\"color:#475569;font-size:0.75rem;white-space:nowrap\">${c.last_synced_at ? new Date(c.last_synced_at).toLocaleString('ru', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</td>
        <td><div class=\"chat-actions\">${approveBtn}${disableBtn}${syncNowBtn}${cancelBtn}${deleteBtn}</div></td>
      </tr>`);
      for (const t of topics) {
        rows.push(`<tr class=\"topic-row\" data-parent=\"${c.id}\" style=\"display:none\">
          <td></td><td></td>
          <td colspan=\"6\"><span class=\"topic-title${t.is_closed ? ' topic-closed' : ''}\">└ ${esc(t.title)}</span></td>
          <td></td>
        </tr>`);
      }
    }
    tbody.innerHTML = rows.join('');
  }

  const pg = document.getElementById('chat-pagination');
  if (pageCount <= 1) {
    pg.innerHTML = `<span class=\"page-info\">${total} чатов</span>`;
    return;
  }
  let html = `<span class=\"page-info\">${total} чатов</span>`;
  html += `<button class=\"page-btn\" data-nav=\"prev\" ${_page === 0 ? 'disabled' : ''}>‹</button>`;
  const start = Math.max(0, _page - 2);
  const end = Math.min(pageCount, start + 5);
  for (let i = start; i < end; i++) {
    html += `<button class=\"page-btn ${i === _page ? 'active' : ''}\" data-page=\"${i}\">${i + 1}</button>`;
  }
  html += `<button class=\"page-btn\" data-nav=\"next\" data-max=\"${pageCount - 1}\" ${_page === pageCount - 1 ? 'disabled' : ''}>›</button>`;
  pg.innerHTML = html;
}

export async function toggleSync() {
  const btn = document.getElementById('sync-toggle-btn');
  const running = btn.dataset.running === '1';
  if (running) await apiFetch('/api/admin/sync/stop', { method: 'POST' });
  else await apiFetch('/api/admin/sync/start', { method: 'POST' });
}

function _updateSyncBtn(running) {
  const btn = document.getElementById('sync-toggle-btn');
  if (!btn) return;
  if (running) {
    btn.textContent = '■';
    btn.title = 'Остановить синхронизацию';
    btn.dataset.running = '1';
    btn.style.color = '#ef4444';
  } else {
    btn.textContent = '▶';
    btn.title = 'Запустить синхронизацию';
    btn.dataset.running = '0';
    btn.style.color = '';
  }
}

export async function pollSync() {
  clearInterval(_syncPollTimer);
  const refresh = async () => {
    const [sr, qr] = await Promise.all([
      apiFetch('/api/admin/sync/status'),
      apiFetch('/api/admin/sync/queue'),
    ]);
    if (!sr.ok) return;
    const s = await sr.json();
    const bar = document.getElementById('sync-bar');
    _updateSyncBtn(s.running);
    if (s.running) {
      bar.classList.add('visible');
      document.getElementById('sync-bar-text').textContent =
        `Синхронизация… чат: ${s.current_chat || '—'} | обработано: ${s.chats_done} чатов, ${s.messages_saved} сообщений`;
    } else {
      bar.classList.remove('visible');
    }
    if (qr.ok) {
      const q = await qr.json();
      const wrap = document.getElementById('sync-queue-wrap');
      const list = document.getElementById('sync-queue-list');
      const cnt = document.getElementById('sync-queue-count');
      wrap.style.display = '';
      cnt.textContent = q.queue.length;
      list.innerHTML = q.queue.map(c => `
        <div class=\"queue-item ${c.active ? 'queue-item-active' : ''}\">
          <span class=\"chat-type-badge\">${esc(c.type)}</span>
          ${c.active ? '<span class=\"spinner\" style=\"width:10px;height:10px;border-width:2px\"></span>' : ''}
          <span>${esc(c.title)}</span>
          <span style=\"margin-left:auto;color:#475569\">${c.depth_days ? c.depth_days + 'д' : 'глоб.'}</span>
        </div>`).join('');
      if (q.running) {
        const active = list.querySelector('.queue-item-active');
        if (active) active.scrollIntoView({ block: 'nearest' });
      }
    }
  };
  await refresh();
  _syncPollTimer = setInterval(refresh, 3000);
}
