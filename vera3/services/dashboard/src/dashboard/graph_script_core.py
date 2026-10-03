"""Ядро скрипта «Людей»: Cytoscape, стили, подсветка соседей, загрузка графа.

Строки, а не f-string: фигурные скобки JS не экранируются. Подставляется один
плейсхолдер — `__PRED_LABELS__` (русские подписи типов связей), его заменяет
`graph_page`. Цвета берутся из CSS-переменных темы, чтобы граф не жил своей палитрой.

Про промахи мышью: Cytoscape считает координаты указателя от КЭШИРОВАННОГО
положения контейнера и от размера своего холста; пересчитываются они только на
`resize` окна и `cy.resize()`. Раньше боковая панель отнимала у холста ширину
(flex) без вызова `cy.resize()`, и клик попадал в узел, нарисованный в другом
месте. Теперь панель плавает поверх холста и размер контейнера не меняет, а
`ResizeObserver` пересчитывает холст при ЛЮБОМ изменении размера (меню, шрифты,
поворот экрана).
"""
from __future__ import annotations

CORE_SCRIPT = r"""
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = {line: css('--line-strong'), muted: css('--muted'), accent: css('--accent'),
           ok: css('--ok'), warn: css('--warn'), text: css('--text'), bg: css('--bg')};
const TYPE_COLOR = {person: C.accent, group: C.warn, supergroup: C.warn, channel: C.ok};
const TYPE_RU = {person:'Человек', group:'Группа', supergroup:'Группа', channel:'Канал',
                 organization:'Организация', account:'Аккаунт', project:'Проект', place:'Место'};
const SOURCE_RU = {telegram:'Telegram', gmail:'Gmail', slack:'Slack', instagram:'Instagram'};
// Тематические кластеры Веры — данные, а не тема: цвет узла = номер сообщества.
const CLUSTER_COLORS = ['#8b8cff','#5fd69b','#f783ac','#ffa94d','#b197fc','#3bc9db',
                        '#ffd43b','#ff8787','#63e6be','#d0a2ff','#e599f7','#a9e34b'];
const clusterColor = c => (c === null || c === undefined) ? null
                        : CLUSTER_COLORS[c % CLUSTER_COLORS.length];
const ringColor = e => clusterColor(e.data('cluster')) || TYPE_COLOR[e.data('type')] || C.muted;

// Имена и ярлыки приходят из Telegram, Gmail и LLM — всё в innerHTML экранируем.
function esc(s){
  return String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',
    '"':'&quot;',"'":'&#39;'}[ch]));
}
const PRED_LABELS = __PRED_LABELS__;
const predLabel = p => PRED_LABELS[p] || String(p||'').replace(/_/g,' ');
const $ = id => document.getElementById(id);
const info = $('g-info'), count = $('g-count'), legend = $('g-legend'), panel = $('g-panel');
const nodeSize = e => 16 + Math.min(40, Math.sqrt(e.data('degree')||1) * 4.5);

const cy = cytoscape({
  container: $('cy'), wheelSensitivity: 0.25, minZoom: 0.12, maxZoom: 3.5,
  style: [
    {selector:'node', style:{
      'background-color': C.bg, 'background-image': e => '/entities/' + e.data('raw') + '/avatar',
      'background-fit':'cover', 'background-clip':'node', 'width': nodeSize, 'height': nodeSize,
      'border-width': 2, 'border-color': ringColor, 'border-opacity': 0.9,
      'label':'data(name)', 'color':C.text, 'font-size':11, 'font-family':'ui-sans-serif, system-ui, sans-serif',
      'text-valign':'bottom', 'text-margin-y':5, 'text-wrap':'ellipsis', 'text-max-width':'110px',
      'text-background-color':C.bg, 'text-background-opacity':0.78, 'text-background-padding':2,
      'text-background-shape':'roundrectangle', 'min-zoomed-font-size':14,
      'transition-property':'opacity, border-width', 'transition-duration':'0.18s'}},
    // У крупных узлов подпись видна и на дальнем плане, у мелких — только при приближении.
    {selector:'node.hub', style:{'min-zoomed-font-size':6, 'font-weight':600}},
    {selector:'node.hl', style:{'border-width':3.5, 'min-zoomed-font-size':0, 'z-index':999,
      'border-opacity':1}},
    {selector:'node:selected', style:{'border-width':4, 'border-color':'#fff', 'min-zoomed-font-size':0,
      'z-index':1000, 'overlay-color':C.accent, 'overlay-opacity':0.18, 'overlay-padding':6}},
    {selector:'node.dim', style:{'opacity':0.12}},
    // Ребро — пара людей целиком: толщина = вес связи.
    {selector:'edge', style:{'width': e => 0.7 + (e.data('weight')||e.data('confidence')||0.3) * 3.6,
      'line-color':C.line, 'curve-style':'haystack', 'haystack-radius':0, 'opacity':0.55,
      'transition-property':'opacity', 'transition-duration':'0.18s'}},
    // Членство — структурная связь: тоньше и пунктиром, чтобы факты выделялись.
    {selector:'edge[predicate = "member_of"]', style:{'line-style':'dashed', 'width':0.6,
      'opacity':0.3, 'curve-style':'straight'}},
    // Выведенная из общения связь — пунктиром: за ней нет ни одной фразы.
    {selector:'edge[?inferred]', style:{'line-style':'dashed'}},
    {selector:'edge.hl', style:{'line-color':C.accent, 'opacity':1, 'z-index':900}},
    {selector:'edge.dim', style:{'opacity':0.04}},
  ],
});
new ResizeObserver(() => cy.resize()).observe($('cy'));
// Корень промахов: Cytoscape держит в кэше положение контейнера на странице и
// сбрасывает его только на resize/scroll окна. Любой сдвиг вёрстки выше холста
// (подпись, легенда, раскрытое меню) оставлял кэш устаревшим, и указатель
// «видел» узлы со смещением в десятки пикселей. Сбрасываем кэш при входе и нажатии.
const refreshBounds = () => cy.renderer().invalidateContainerClientCoordsCache();
for (const type of ['pointerenter', 'pointerdown', 'wheel', 'touchstart'])
  $('cy').addEventListener(type, refreshBounds, {capture: true, passive: true});
window.addEventListener('scroll', refreshBounds, {passive: true});

function highlight(node){
  cy.batch(() => {
    cy.elements().removeClass('hl dim');
    if (!node || node.empty()) return;
    const around = node.closedNeighborhood();
    cy.elements().not(around).addClass('dim');
    around.addClass('hl');
  });
}
const selectedNode = () => cy.nodes(':selected');
cy.on('mouseover', 'node', ev => { $('cy').style.cursor = 'pointer'; highlight(ev.target); });
cy.on('mouseout', 'node', () => { $('cy').style.cursor = ''; highlight(selectedNode()); });

function selectNode(id){
  const n = cy.getElementById('n' + id);
  if (n.empty()) return false;
  cy.nodes(':selected').unselect();
  n.select();
  highlight(n);
  cy.animate({center:{eles:n}, zoom:Math.max(cy.zoom(), 1.3)}, {duration:420, easing:'ease-out-cubic'});
  return true;
}

function fmtStamp(iso){
  const d = new Date(String(iso).replace(/Z?$/, 'Z'));
  if (isNaN(d)) return String(iso);
  const p = n => String(n).padStart(2, '0');
  return p(d.getDate()) + '.' + p(d.getMonth()+1) + ' в ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

let lastParams = null;
function render(data){
  const els = [];
  const degrees = data.nodes.map(n => n.degree).sort((x, y) => y - x);
  const hubFrom = Math.max(3, degrees[Math.floor(degrees.length * 0.12)] || 0);
  for (const n of data.nodes)
    els.push({classes: (n.degree >= hubFrom ? 'hub' : ''),
              data:{id:'n'+n.id, name:n.name, type:n.type, degree:n.degree, raw:n.id,
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
             nodeOverlap:10, gravity:60, padding:40}).run();
  cy.fit(undefined, 40);
  renderLegend(data);
  count.textContent = data.nodes.length + ' сущностей · ' + data.edges.length + ' связей';
  if (data.focus_id){
    const f = data.nodes.find(n => n.id === data.focus_id);
    info.textContent = 'Фокус: ' + (f ? f.name : '#' + data.focus_id) + ' и окружение';
    selectNode(data.focus_id);
    return;
  }
  info.textContent = 'Ядро графа — самые связанные. Наведите на узел, нажмите — откроется карточка.';
}

function load(params){
  lastParams = params;
  const qs = new URLSearchParams(params).toString();
  $('cy').classList.add('loading');
  return fetch('/api/graph?' + qs, {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(data => { render(data); return data; })
    .catch(err => { info.textContent = 'Ошибка загрузки графа: ' + err; })
    .finally(() => $('cy').classList.remove('loading'));
}
const coreParams = () => ({min_degree: $('g-mindeg').value, predicate: $('g-pred').value, limit: 300});
const focusParams = id => ({focus: id, predicate: $('g-pred').value, limit: 400});
"""
