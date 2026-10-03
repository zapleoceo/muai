"""Скрипт страницы «Люди»: граф Cytoscape и боковая панель сущности.

Строка, а не f-string: фигурные скобки JS не экранируются. Подставляется один
плейсхолдер — `__PRED_LABELS__` (русские подписи типов связей). Цвета холста
берутся из CSS-переменных темы, чтобы граф не жил своей палитрой.
"""
from __future__ import annotations

GRAPH_SCRIPT = r"""
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = {line: css('--vera-line'), muted: css('--vera-muted'), primary: css('--pico-primary'),
           ok: css('--vera-ok'), warn: css('--vera-warn'), text: css('--pico-color')};
const TYPE_COLOR = {person: C.primary, group: C.warn, supergroup: C.warn, channel: C.ok};
const TYPE_RU = {person:'Человек', group:'Группа', supergroup:'Группа', channel:'Канал',
                 organization:'Организация', account:'Аккаунт', project:'Проект', place:'Место'};
const SOURCE_RU = {telegram:'Telegram', gmail:'Gmail', slack:'Slack', instagram:'Instagram'};
// Тематические кластеры Веры — данные, а не тема: цвет узла = номер сообщества.
const CLUSTER_COLORS = ['#4dabf7','#69db7c','#f783ac','#ffa94d','#9775fa','#3bc9db',
                        '#ffd43b','#ff8787','#63e6be','#b197fc','#e599f7','#a9e34b'];
const clusterColor = c => (c === null || c === undefined) ? null
                        : CLUSTER_COLORS[c % CLUSTER_COLORS.length];

// Имена и ярлыки приходят из Telegram, Gmail и LLM — всё в innerHTML экранируем.
function esc(s){
  return String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',
    '"':'&quot;',"'":'&#39;'}[ch]));
}
const PRED_LABELS = __PRED_LABELS__;
const predLabel = p => PRED_LABELS[p] || String(p||'').replace(/_/g,' ');
const $ = id => document.getElementById(id);
const info = $('g-info'), count = $('g-count'), legend = $('g-legend'), panel = $('g-panel');

const cy = cytoscape({
  container: $('cy'), wheelSensitivity: 0.2,
  style: [
    {selector:'node', style:{
      'background-color': e => clusterColor(e.data('cluster')) || TYPE_COLOR[e.data('type')] || C.muted,
      'background-image': e => '/entities/' + e.data('raw') + '/avatar',
      'background-fit':'cover', 'background-clip':'node',
      'label':'data(name)', 'color':C.text, 'font-size':'9px',
      'text-wrap':'ellipsis', 'text-max-width':'90px', 'text-margin-y':'-3px',
      'width': e => 10 + Math.min(44, Math.sqrt(e.data('degree')||1)*4),
      'height': e => 10 + Math.min(44, Math.sqrt(e.data('degree')||1)*4),
      'border-width': e => e.data('cluster')!==null && e.data('cluster')!==undefined ? 3 : 1,
      'border-color': e => clusterColor(e.data('cluster')) || C.line}},
    {selector:'node:selected', style:{'border-width':4, 'border-color':C.text,
      'font-size':'12px'}},
    // Ребро — пара людей целиком: толщина = вес связи, подпись при клике.
    {selector:'edge', style:{'width': e => 0.6 + (e.data('weight')||e.data('confidence')||0.3)*3.4,
      'line-color':C.line, 'curve-style':'haystack', 'opacity':0.6}},
    // Членство — структурная связь: тоньше и пунктиром, чтобы факты выделялись.
    {selector:'edge[predicate = "member_of"]', style:{'line-style':'dashed', 'width':0.5,
      'opacity':0.35, 'curve-style':'straight'}},
    // Выведенная из общения связь — пунктиром: за ней нет ни одной фразы.
    {selector:'edge[?inferred]', style:{'line-style':'dashed'}},
    {selector:'edge:selected', style:{'line-color':C.primary,'opacity':1,'width':2}},
  ],
});

function fmtStamp(iso){
  const d = new Date(String(iso).replace(/Z?$/, 'Z'));
  if (isNaN(d)) return String(iso);
  const p = n => String(n).padStart(2, '0');
  return p(d.getDate()) + '.' + p(d.getMonth()+1) + ' в ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function renderLegend(data){
  legend.innerHTML = '';
  const labels = data.cluster_labels || {};
  const present = new Set(data.nodes.map(n=>n.cluster).filter(c=>c!==null&&c!==undefined));
  const items = Object.keys(labels).map(Number).filter(c=>present.has(c)).sort((a,b)=>a-b);
  if (!items.length){
    if (data.recluster_running)
      legend.innerHTML = '<span class="pill warn">Вера раскрашивает граф по темам — обнови страницу через минуту</span>';
    else if (!data.clusters_at)
      legend.innerHTML = '<span class="muted small">«Раскрасить по темам» (в фильтрах) — Вера разобьёт граф на темы и покрасит узлы. Меняются только цвета, данные и фильтры — нет.</span>';
    return;
  }
  if (data.clusters_at)
    legend.insertAdjacentHTML('beforeend', '<span class="muted small">темы посчитаны ' +
      esc(fmtStamp(data.clusters_at)) + ' · цвет узла = тема</span>');
  for (const c of items)
    legend.insertAdjacentHTML('beforeend', '<span class="chip"><span class="swatch" style="background:' +
      clusterColor(c) + '"></span>' + esc(labels[c]) + '</span>');
}

function tgLink(u, id){
  if (u) return 'https://t.me/' + String(u).replace(/^@/, '');
  if (id) return 'tg://user?id=' + id;
  return null;
}

function render(data){
  const els = [];
  for (const n of data.nodes)
    els.push({data:{id:'n'+n.id, name:n.name, type:n.type, degree:n.degree, raw:n.id,
                     username:n.username, tg_id:n.tg_id,
                     cluster:(n.cluster===undefined?null:n.cluster)}});
  const seen = new Set(data.nodes.map(n=>'n'+n.id));
  for (const e of data.edges){
    const s='n'+e.source, t='n'+e.target;
    if (seen.has(s) && seen.has(t))
      els.push({data:{id:s+'_'+t+'_'+e.predicate, source:s, target:t,
                       predicate:e.predicate, confidence:e.confidence,
                       weight:e.weight, also:e.also||[], inferred:!!e.inferred}});
  }
  cy.elements().remove();
  cy.add(els);
  cy.layout({name:'cose', animate:false, nodeRepulsion:9000, idealEdgeLength:70,
             nodeOverlap:10, gravity:60}).run();
  renderLegend(data);
  count.textContent = data.nodes.length + ' сущностей, ' + data.edges.length + ' связей';
  if (data.focus_id){
    const f = data.nodes.find(n => n.id === data.focus_id);
    info.textContent = 'Фокус на «' + (f ? f.name : '#' + data.focus_id) +
                       '» и её соседях. «↺ весь граф» (в фильтрах) — назад.';
    return;
  }
  info.textContent = 'Ядро графа (самые связанные). Клик по узлу — карточка и окружение.';
}

function load(params){
  const qs = new URLSearchParams(params).toString();
  return fetch('/api/graph?' + qs, {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(data => { render(data); return data; })
    .catch(err => { info.textContent = 'Ошибка загрузки графа: ' + err; });
}

const coreParams = () => ({min_degree: $('g-mindeg').value, predicate: $('g-pred').value, limit: 300});
const focusParams = id => ({focus: id, predicate: $('g-pred').value, limit: 400});

function row(label, html){ return html ? '<li>' + label + html + '</li>' : ''; }

function renderPanel(p){
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
  const stats = [c.relationships + ' связей'];
  if (c.groups) stats.push('в группах: ' + c.groups);
  if (c.members) stats.push('участников: ' + c.members);
  const rels = p.connections.map(connRow).join('');
  const evs = p.events.map(e =>
    '<li><a href="/events/' + e.id + '">' + esc(SOURCE_RU[e.source] || e.source) + ' · ' +
    esc(fmtStamp(e.occurred_at)) + '</a><div class="muted small">' + esc(e.snippet) + '</div></li>').join('');
  panel.innerHTML =
    '<div class="g-panel-head"><img src="/entities/' + p.id + '/avatar" alt="">' +
    '<div><h3>' + esc(p.name) + '</h3><span class="muted small">' +
    esc(TYPE_RU[p.type] || p.type) + '</span></div>' +
    '<button type="button" class="secondary g-close" aria-label="Закрыть">✕</button></div>' +
    (p.profile.length ? '<p class="muted small">' + p.profile.map(esc).join(' · ') + '</p>' : '') +
    (chips.length ? '<div class="g-chips">' + chips.map(x => '<span class="chip">' + x + '</span>').join('') + '</div>' : '') +
    '<p class="muted small">' + stats.join(' · ') + '</p>' +
    (rels ? '<h4>Связи по людям</h4><ul>' + rels + '</ul>' : '') +
    (evs ? '<h4>Последние события</h4><ul>' + evs + '</ul>' : '') +
    (link ? '<a role="button" class="secondary" href="' + esc(link) + '"' +
      (link.indexOf('http') === 0 ? ' target="_blank" rel="noopener"' : '') + '>Открыть в Telegram</a>' : '');
}

function openPanel(id){
  panel.hidden = false;
  panel.innerHTML = '<p class="muted">Загружаю…</p>';
  fetch('/api/graph/entity/' + id, {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(renderPanel)
    .catch(err => { panel.innerHTML = '<p class="err">Карточка не загрузилась: ' + esc(err) + '</p>'; });
}

function focusOn(id){
  info.textContent = 'Загружаю окружение…';
  openPanel(id);
  load(focusParams(id));
}

cy.on('tap', 'node', ev => focusOn(ev.target.data('raw')));
cy.on('tap', 'edge', ev => {
  const e = ev.target;
  const also = (e.data('also') || []).map(predLabel).join(', ');
  const w = e.data('weight');
  info.textContent = e.source().data('name') + ' — ' + predLabel(e.data('predicate')) +
                     ' — ' + e.target().data('name') + (w ? ' (вес ' + w.toFixed(2) + ')' : '') +
                     (also ? ' · также: ' + also : '') +
                     (e.data('inferred') ? ' (выведено из общения)' : '');
});
panel.addEventListener('click', ev => {
  const focus = ev.target.closest('[data-focus]');
  if (focus){ ev.preventDefault(); focusOn(focus.dataset.focus); return; }
  if (ev.target.closest('.g-close')) panel.hidden = true;
});
$('g-reset').onclick = () => { panel.hidden = true; load(coreParams()); };
$('g-mindeg').onchange = () => load(coreParams());
$('g-pred').onchange = () => load(coreParams());
$('g-searchform').addEventListener('submit', ev => {
  ev.preventDefault();
  const q = $('g-search').value.trim();
  if (!q) return;
  info.textContent = 'Ищу «' + q + '»…';
  load({q: q, predicate: $('g-pred').value, limit: 400}).then(data => {
    if (data && data.focus_id) openPanel(data.focus_id);
  });
});

load(coreParams());
"""
