"""Скрипт карточки человека в «Людях»: отрисовка, загрузка, разрыв связи с откатом.

Разрыв — три шага: диалог подтверждения (`VeraUI.confirm`), POST в дашборд
(`/api/graph/connection/break` или `/reject`), тост «Вернуть» с откатом через
`/api/journal/undo`. Тексты вопроса идут через `textContent`, экранировать их не нужно.
"""
from __future__ import annotations

PANEL_SCRIPT = r"""
function tgLink(u, id){
  if (u) return 'https://t.me/' + String(u).replace(/^@/, '');
  if (id) return 'tg://user?id=' + id;
  return null;
}

let current = null;   // карточка, которая открыта сейчас: из неё берутся индексы кнопок

function statTile(n, label){
  return '<div class="g-stat"><b>' + n + '</b><span>' + label + '</span></div>';
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
  const tiles = statTile(c.relationships, 'связей') + (c.groups ? statTile(c.groups, 'групп') : '') +
                (c.members ? statTile(c.members, 'участников') : '');
  const rels = p.connections.map(connRow).join('');
  const evs = p.events.map(e =>
    '<li><a href="/events/' + e.id + '">' + esc(SOURCE_RU[e.source] || e.source) + ' · ' +
    esc(fmtStamp(e.occurred_at)) + '</a>' + (e.subject ? '<div class="small"><b>' + esc(e.subject) + '</b></div>' : '') +
    '<div class="muted small">' + esc(e.snippet) + '</div></li>').join('');
  panel.innerHTML =
    '<div class="g-panel-head"><img src="/entities/' + p.id + '/avatar" alt="">' +
    '<div><h3>' + esc(p.name) + '</h3><span class="muted small">' +
    esc(TYPE_RU[p.type] || p.type) + '</span></div>' +
    '<button type="button" class="ghost g-close" aria-label="Закрыть">✕</button></div>' +
    (p.profile.length ? '<p class="muted small">' + p.profile.map(esc).join(' · ') + '</p>' : '') +
    (chips.length ? '<div class="g-chips">' + chips.map(x => '<span class="chip">' + x + '</span>').join('') + '</div>' : '') +
    '<div class="g-stats">' + tiles + '</div>' +
    '<h4>Связи по людям</h4>' + (rels ? '<ul class="g-list">' + rels + '</ul>' : '<p class="muted small">Связей пока нет.</p>') +
    '<button type="button" class="secondary sm" data-act="addrel">＋ Указать связь</button>' +
    (evs ? '<h4>Последние события</h4><ul class="g-list">' + evs + '</ul>' : '') +
    '<div class="g-foot"><button type="button" class="secondary" data-focus-net="' + p.id + '">Показать окружение</button>' +
    (link ? '<a role="button" class="secondary" href="' + esc(link) + '"' +
      (link.indexOf('http') === 0 ? ' target="_blank" rel="noopener"' : '') + '>Открыть в Telegram</a>' : '') + '</div>';
}

// Токен запроса: медленный ответ про прежнего человека не должен затереть новую карточку.
let panelSeq = 0;

function openPanel(id, highlightOther){
  const seq = ++panelSeq;
  panel.hidden = false;
  panel.classList.add('open');
  panel.innerHTML = '<div class="skel-stack"><div class="skeleton"></div><div class="skeleton"></div>' +
                    '<div class="skeleton"></div></div>';
  return fetch('/api/graph/entity/' + id, {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(p => {
      if (seq !== panelSeq) return;
      renderPanel(p);
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

function confirmEdit(act, c, r){
  const label = r.label || predLabel(r.predicate);
  const pair = c.other_name + ' — ' + label + ' — ' + current.name;
  const breaking = act === 'break';
  return VeraUI.confirm({
    title: breaking ? 'Разорвать связь?' : 'Это неверная связь?',
    message: breaking ? 'Связь ' + pair + ' будет погашена; вернуть можно в журнале.'
      : 'Вера перестанет считать общение уликой связи ' + pair + '; вернуть можно в журнале.',
    confirmLabel: breaking ? 'Разорвать' : 'Отметить неверной', danger: true});
}

function editConnection(btn){
  const c = current.connections[Number(btn.dataset.conn)];
  const r = Number(btn.dataset.role) === 0 ? c.main : c.also[Number(btn.dataset.role) - 1];
  const act = btn.dataset.act;
  confirmEdit(act, c, r).then(ok => {
    if (!ok) return;
    const pair = {entity_a: current.id, entity_b: c.other_id};
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
  if (act && act.dataset.act === 'addrel'){ addRelationship(); return; }
  if (act){ editConnection(act); return; }
  const focus = ev.target.closest('[data-focus]');
  if (focus){ ev.preventDefault(); focusOn(focus.dataset.focus); return; }
  const net = ev.target.closest('[data-focus-net]');
  if (net){ load(focusParams(net.dataset.focusNet)); return; }
  if (ev.target.closest('.g-close')) closePanel();
});
"""
