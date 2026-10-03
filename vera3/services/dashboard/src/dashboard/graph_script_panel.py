"""Скрипт карточки человека в «Людях»: отрисовка, загрузка, правка связей с откатом.

Карточка рисуется сразу (скелетон до первого ответа), а «Последние события» грузятся
отдельным запросом: поиск по алиасам медленнее остального. Правки — через диалоги
`VeraUI.choose` (они же подтверждение), POST в дашборд и тост «Вернуть» с откатом через
`/api/journal/undo`. Тексты вопросов идут через `textContent`, экранировать их не нужно.
"""
from __future__ import annotations

PANEL_SCRIPT = r"""
function tgLink(u, id){
  if (u) return 'https://t.me/' + String(u).replace(/^@/, '');
  if (id) return 'tg://user?id=' + id;
  return null;
}

let current = null;   // карточка, которая открыта сейчас: из неё берутся индексы кнопок
let lastEvents = null;
let panelSeq = 0;     // токен запроса: медленный ответ про прежнего человека не затирает новую карточку

const statTile = (n, label) => '<div class="g-stat"><b>' + n + '</b><span>' + label + '</span></div>';
const SKELETON = '<div class="skel-stack"><div class="skeleton"></div><div class="skeleton"></div>' +
                 '<div class="skeleton"></div></div>';

function eventsHtml(list){
  if (list === null) return '<div class="skel-stack"><div class="skeleton"></div><div class="skeleton"></div></div>';
  if (!list.length) return '<p class="muted small">Событий не нашлось.</p>';
  return '<ul class="g-list">' + list.map(e =>
    '<li><a href="/events/' + e.id + '">' + esc(SOURCE_RU[e.source] || e.source) + ' · ' +
    esc(fmtStamp(e.occurred_at)) + '</a>' + (e.subject ? '<div class="small"><b>' + esc(e.subject) + '</b></div>' : '') +
    '<div class="muted small">' + esc(e.snippet) + '</div></li>').join('') + '</ul>';
}

function renderPanel(p){
  current = p;
  const link = tgLink(p.username, p.tg_id);
  const chips = [];
  if (p.username) chips.push('@' + esc(p.username));
  if (p.email) chips.push(esc(p.email));
  for (const a of p.aliases){
    const id = String(a.identifier).replace(/^user:/, '');
    if (id !== p.username && id !== p.email)
      chips.push(esc(SOURCE_RU[a.source] || a.source) + ': ' + esc(id));
  }
  const c = p.counts;
  const total = p.connections_total === undefined ? p.connections.length : p.connections_total;
  const tiles = statTile(total, 'людей в связях') + (c.groups ? statTile(c.groups, 'групп') : '') +
                (c.members ? statTile(c.members, 'участников') : '');
  const rels = p.connections.map(connRow).join('');
  const more = total > p.connections.length
    ? '<button type="button" class="ghost sm" data-act="moreconns">и ещё ' + (total - p.connections.length) + ' — показать все</button>' : '';
  panel.innerHTML =
    '<div class="g-panel-head"><img src="/entities/' + p.id + '/avatar" alt="">' +
    '<div><h3>' + esc(p.name) + '</h3><span class="muted small">' +
    esc(TYPE_RU[p.type] || p.type) + '</span></div>' +
    '<button type="button" class="ghost g-close" aria-label="Закрыть">✕</button></div>' +
    (p.profile.length ? '<p class="muted small">' + p.profile.map(esc).join(' · ') + '</p>' : '') +
    (chips.length ? '<div class="g-chips">' + chips.map(x => '<span class="chip">' + x + '</span>').join('') + '</div>' : '') +
    '<div class="g-stats">' + tiles + '</div>' +
    '<h4>Связи по людям</h4>' + (rels ? '<ul class="g-list">' + rels + '</ul>' : '<p class="muted small">Связей пока нет.</p>') +
    more + '<button type="button" class="secondary sm" data-act="addrel">＋ Указать связь</button>' +
    '<h4>Последние события</h4><div id="g-events">' + eventsHtml(lastEvents) + '</div>' +
    '<div class="g-foot"><button type="button" class="secondary" data-focus-net="' + p.id + '">Показать окружение</button>' +
    (link ? '<a role="button" class="secondary" href="' + esc(link) + '"' +
      (link.indexOf('http') === 0 ? ' target="_blank" rel="noopener"' : '') + '>Открыть в Telegram</a>' : '') + '</div>';
}

const getJson = url => fetch(url, {credentials:'same-origin'}).then(r => r.ok ? r.json() : Promise.reject(r.status));

function loadEvents(id, seq){
  getJson('/api/graph/entity/' + id + '/events').then(d => {
    if (seq !== panelSeq) return;
    lastEvents = d.events;
    const box = $('g-events');
    if (box) box.innerHTML = eventsHtml(lastEvents);
  }).catch(() => { const box = $('g-events'); if (box && seq === panelSeq) box.innerHTML = '<p class="muted small">События не загрузились.</p>'; });
}

function openPanel(id, highlightOther, showAll){
  const seq = ++panelSeq;
  lastEvents = null;
  panel.hidden = false;
  panel.classList.add('open');
  panel.innerHTML = SKELETON;
  return getJson('/api/graph/entity/' + id + '?events=0' + (showAll ? '&all=1' : ''))
    .then(p => {
      if (seq !== panelSeq) return;
      renderPanel(p);
      loadEvents(id, seq);
      const row = highlightOther && panel.querySelector('[data-other="' + highlightOther + '"]');
      if (row){ row.classList.add('flash'); row.scrollIntoView({block:'center', behavior:'smooth'}); }
    })
    .catch(err => { if (seq === panelSeq) panel.innerHTML = '<p class="err">Карточка не загрузилась: ' + esc(err) + '</p>'; });
}

function closePanel(){
  panelSeq++;
  panel.classList.remove('open');
  panel.hidden = true;
  current = null;
  cy.nodes(':selected').unselect();
  highlight(null);
}

function refreshAfterEdit(){
  if (current) openPanel(current.id);
  if (lastParams) load(lastParams);
}

function afterEdit(message, auditIds){
  const action = auditIds.length ? {label: 'Вернуть', run: () =>
    VeraUI.post('/api/journal/undo', {audit_ids: auditIds})
      .then(() => { VeraUI.toast('Связь возвращена', {kind: 'ok'}); refreshAfterEdit(); })
      .catch(err => VeraUI.toast('Не получилось вернуть: ' + err.message, {kind: 'err'}))} : null;
  VeraUI.toast(message, {kind: 'ok', action: action, ttl: 12000});
  refreshAfterEdit();
}

// Одна кнопка на связь: в диалоге выбирается, что именно сделать с какой ролью.
function manageConnection(btn){
  const c = current.connections[Number(btn.dataset.conn)];
  const choices = [];
  connRoles(c).forEach((r, ri) => {
    const label = r.label || predLabel(r.predicate);
    if (r.rel_ids && r.rel_ids.length) choices.push({value: 'break:' + ri, label: 'Разорвать: ' + label});
    if (r.inferred) choices.push({value: 'reject:' + ri, label: 'Это неверно (выведено из общения): ' + label});
  });
  const pairOf = ri => c.other_name + ' — ' + (connRoles(c)[ri].label || predLabel(connRoles(c)[ri].predicate)) + ' — ' + current.name;
  const describe = v => {
    const [act, ri] = v.what.split(':');
    return act === 'break' ? 'Связь ' + pairOf(Number(ri)) + ' будет погашена; вернуть можно в журнале.'
      : 'Вера перестанет считать общение уликой связи ' + pairOf(Number(ri)) + '; вернуть можно в журнале.';
  };
  const viewed = current;
  VeraUI.choose({title: 'Изменить связь с ' + c.other_name, confirmLabel: 'Подтвердить', danger: true,
    fields: [{name: 'what', label: 'Что сделать', options: choices}],
    refresh: v => ({message: describe(v)})}).then(v => {
    if (!v) return;
    const [act, ri] = v.what.split(':');
    const r = connRoles(c)[Number(ri)];
    const pair = {entity_a: viewed.id, entity_b: c.other_id};
    const req = act === 'break' ? VeraUI.post('/api/graph/connection/break', {...pair, predicate: r.predicate, rel_ids: r.rel_ids})
                                : VeraUI.post('/api/graph/connection/reject', pair);
    req.then(res => afterEdit(act === 'break' ? 'Связь разорвана' : 'Связь отмечена неверной', res.audit_ids))
       .catch(err => VeraUI.toast('Не получилось: ' + err.message, {kind: 'err'}));
  });
}

const MANUAL_ROLES = __MANUAL_ROLES__;

// «Указать связь»: пара — открытая карточка и якорь (владелец или одна из её связей).
function addRelationship(){
  const anchors = [];
  if (current.owner_id && current.owner_id !== current.id)
    anchors.push({value: String(current.owner_id), label: (current.owner_name || 'Я') + ' (я)'});
  for (const c of current.connections)
    if (c.other_id !== current.owner_id) anchors.push({value: String(c.other_id), label: c.other_name});
  if (!anchors.length){ VeraUI.toast('Не с кем связывать: у карточки нет собеседников.', {kind: 'err'}); return; }
  const names = Object.fromEntries(anchors.map(a => [a.value, a.label.replace(/ \(я\)$/, '')]));
  const roles = anchor => MANUAL_ROLES.map(r => ({value: r.key,
    label: r.text.replace('{x}', current.name).replace('{y}', names[anchor])}));
  const describe = v => 'Будет записано: ' + roles(v.anchor).find(r => r.value === v.role).label +
    '. Связь получит максимальный вес; вернуть можно в журнале.';
  const viewed = current;
  VeraUI.choose({
    title: 'Указать связь', confirmLabel: 'Указать',
    fields: [{name: 'anchor', label: 'С кем', options: anchors},
             {name: 'role', label: 'Какая связь', options: roles(anchors[0].value)}],
    refresh: v => ({message: describe(v), options: {role: roles(v.anchor)}}),
  }).then(v => {
    if (!v) return;
    VeraUI.post('/api/graph/connection/set', {entity_a: viewed.id, entity_b: Number(v.anchor), role: v.role})
      .then(res => afterEdit('Связь указана', res.audit_ids))
      .catch(err => VeraUI.toast('Не получилось: ' + err.message, {kind: 'err'}));
  });
}

function focusOn(id){
  if (selectNode(id)){ openPanel(id); return; }
  info.textContent = 'Загружаю окружение…';
  openPanel(id);
  load(focusParams(id));
}

panel.addEventListener('click', ev => {
  const act = ev.target.closest('[data-act]');
  if (act){
    if (act.dataset.act === 'addrel') addRelationship();
    else if (act.dataset.act === 'manage') manageConnection(act);
    else if (act.dataset.act === 'moreconns') openPanel(current.id, null, true);
    return;
  }
  const focus = ev.target.closest('[data-focus]');
  if (focus){ ev.preventDefault(); focusOn(focus.dataset.focus); return; }
  const net = ev.target.closest('[data-focus-net]');
  if (net){ load(focusParams(net.dataset.focusNet)); return; }
  if (ev.target.closest('.g-close')) closePanel();
});
"""
