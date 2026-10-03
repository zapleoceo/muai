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
// Узел без фото: цветной диск с инициалами. Лежит вторым слоем под настоящим аватаром:
// если картинка не загрузилась (404, группа без фото), остаётся он, а не чёрный кружок.
const INI_TINTS = ['#6d6fe8','#2fa37a','#c75a8a','#c9803a','#7d62d6','#2c9bb0','#b39a2a','#c76060'];
function initialsUri(name, id){
  const ini = String(name).trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="' +
    INI_TINTS[id % INI_TINTS.length] + '"/><text x="32" y="41" font-size="26" font-family="sans-serif" ' +
    'font-weight="600" fill="#fff" text-anchor="middle">' + esc(ini) + '</text></svg>';
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
const nodeSize = e => 16 + Math.min(40, Math.sqrt(e.data('degree')||1) * 4.5);

const cy = cytoscape({
  container: $('cy'), wheelSensitivity: 0.25, minZoom: 0.12, maxZoom: 3.5,
  style: [
    {selector:'node', style:{
      'background-color': e => INI_TINTS[e.data('raw') % INI_TINTS.length],
      'background-image': e => ['/entities/' + e.data('raw') + '/avatar', e.data('ini')],
      'background-fit':'cover', 'background-clip':'node', 'width': nodeSize, 'height': nodeSize,
      'border-width': 2, 'border-color': ringColor, 'border-opacity': 0.9,
      'label':'data(name)', 'color':C.text, 'font-size':11, 'font-family':'ui-sans-serif, system-ui, sans-serif',
      'text-valign':'bottom', 'text-margin-y':5, 'text-wrap':'ellipsis', 'text-max-width':'110px',
      'text-background-color':C.bg, 'text-background-opacity':0.78, 'text-background-padding':2,
      'text-background-shape':'roundrectangle', 'min-zoomed-font-size':14,
      'z-index-compare':'manual', 'z-index':10,
      'transition-property':'opacity, border-width', 'transition-duration':'0.18s'}},
    // У крупных узлов подпись видна и на дальнем плане, у мелких — только при приближении.
    {selector:'node.hub', style:{'min-zoomed-font-size':6, 'font-weight':600}},
    {selector:'node.hl', style:{'border-width':3.5, 'min-zoomed-font-size':0, 'z-index':999,
      'border-opacity':1}},
    {selector:'node:selected', style:{'border-width':4, 'border-color':'#fff', 'min-zoomed-font-size':0,
      'z-index':1000, 'overlay-color':C.accent, 'overlay-opacity':0.18, 'overlay-padding':6}},
    {selector:'node.dim', style:{'opacity':0.12, 'text-opacity':0, 'text-background-opacity':0}},
    // Ребро — пара людей целиком: толщина = вес связи.
    {selector:'edge', style:{'width': e => 0.7 + (e.data('weight')||e.data('confidence')||0.3) * 3.6,
      'line-color':C.line, 'curve-style':'haystack', 'haystack-radius':0, 'opacity':0.55,
      'z-index-compare':'manual', 'z-index':1,
      'transition-property':'opacity', 'transition-duration':'0.18s'}},
    // Членство — структурная связь: тоньше и пунктиром, чтобы факты выделялись.
    {selector:'edge[predicate = "member_of"]', style:{'line-style':'dashed', 'width':0.6,
      'opacity':0.3, 'curve-style':'straight'}},
    // Выведенная из общения связь — пунктиром: за ней нет ни одной фразы.
    {selector:'edge[?inferred]', style:{'line-style':'dashed', 'width':0.7, 'opacity':0.28}},
    // Рёбра всегда ниже узлов: подсвеченное ребро поверх узла перехватывало бы наведение.
    {selector:'edge.hl', style:{'line-color':C.accent, 'opacity':1, 'z-index':5}},
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

const isMobile = () => window.matchMedia('(max-width:760px)').matches;
// Карточка закрывает справа 24 rem (на телефоне — нижние две трети): узел ставим в середину СВОБОДНОЙ части.
function freeCenter(){
  const w = $('cy').clientWidth, h = $('cy').clientHeight;
  if (isMobile()) return {x: w / 2, y: Math.max(70, h * 0.2)};
  return {x: Math.max(120, (w - 410) / 2), y: h / 2};
}
function panToFree(node, zoom){
  const c = freeCenter(), p = node.position();
  cy.animate({zoom: zoom, pan: {x: c.x - zoom * p.x, y: c.y - zoom * p.y}},
             {duration: 420, easing: 'ease-out-cubic'});
}

function selectNode(id){
  const n = cy.getElementById('n' + id);
  if (n.empty()) return false;
  cy.nodes(':selected').unselect();
  n.select();
  highlight(n);
  panToFree(n, Math.max(cy.zoom(), isMobile() ? 1.0 : 1.15));
  return true;
}

function fmtStamp(iso){
  const d = new Date(String(iso).replace(/Z?$/, 'Z'));
  return isNaN(d) ? String(iso) : window.__fmtDate(d, 'datetime');
}

// cose даёт круглое облако; холст широкий — растягиваем по ширине, чтобы использовать его целиком.
function stretchToCanvas(){
  const bb = cy.elements().boundingBox();
  if (bb.w < 1 || bb.h < 1) return;
  const k = Math.min(1.9, ($('cy').clientWidth / $('cy').clientHeight) / (bb.w / bb.h));
  if (k > 1.05) cy.nodes().positions(n => ({x: bb.x1 + (n.position('x') - bb.x1) * k, y: n.position('y')}));
}

let lastParams = null;
function render(data){
  const els = [];
  const degrees = data.nodes.map(n => n.degree).sort((x, y) => y - x);
  const hubFrom = Math.max(3, degrees[Math.floor(degrees.length * 0.12)] || 0);
  for (const n of data.nodes)
    els.push({classes: (n.degree >= hubFrom ? 'hub' : ''),
              data:{id:'n'+n.id, name:n.name, ini:initialsUri(n.name, n.id), type:n.type, degree:n.degree, raw:n.id,
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
  cy.layout({name:'cose', animate:false, randomize:true, nodeRepulsion:26000, idealEdgeLength:120,
             edgeElasticity:90, nodeOverlap:30, gravity:14, numIter:1800, componentSpacing:110,
             padding:40}).run();
  stretchToCanvas();
  cy.fit(undefined, 40);
  if (isMobile() && cy.zoom() < 0.6){ cy.zoom({level: 0.6, renderedPosition: {x: $('cy').clientWidth / 2, y: $('cy').clientHeight / 2}}); }
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
