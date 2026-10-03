"""Скрипт управления «Людьми»: легенда, поиск с подсказками, зум, горячие клавиши,
запуск. Подсказки ищут по узлам, уже лежащим на холсте; Enter без совпадений
отправляет запрос на сервер (он ищет по всей базе)."""
from __future__ import annotations

UI_SCRIPT = r"""
function renderLegend(data){
  legend.innerHTML = '<span class="chip"><i class="ln"></i>записано</span>' +
    '<span class="chip"><i class="ln dashed"></i>выведено из общения</span>' +
    '<span class="chip muted">толщина = вес связи</span>';
  const labels = data.cluster_labels || {};
  const present = new Set(data.nodes.map(n=>n.cluster).filter(c=>c!==null&&c!==undefined));
  const items = Object.keys(labels).map(Number).filter(c=>present.has(c)).sort((a,b)=>a-b);
  if (!items.length){
    if (data.recluster_running)
      legend.insertAdjacentHTML('beforeend', '<span class="pill warn">Вера раскрашивает граф по темам — обновите через минуту</span>');
    return;
  }
  if (data.clusters_at)
    legend.insertAdjacentHTML('beforeend', '<span class="muted small">темы посчитаны ' +
      esc(fmtStamp(data.clusters_at)) + '</span>');
  for (const c of items)
    legend.insertAdjacentHTML('beforeend', '<span class="chip"><span class="swatch" style="background:' +
      clusterColor(c) + '"></span>' + esc(labels[c]) + '</span>');
}

cy.on('tap', 'node', ev => { focusOn(ev.target.data('raw')); });
cy.on('dbltap', 'node', ev => { load(focusParams(ev.target.data('raw'))); });
cy.on('tap', 'edge', ev => {
  const e = ev.target, w = e.data('weight');
  const also = (e.data('also') || []).map(predLabel).join(', ');
  info.textContent = e.source().data('name') + ' — ' + predLabel(e.data('predicate')) +
    ' — ' + e.target().data('name') + (w ? ' (вес ' + w.toFixed(2) + ')' : '') +
    (also ? ' · также: ' + also : '') + (e.data('inferred') ? ' · выведено из общения' : '');
  selectNode(e.source().data('raw'));
  openPanel(e.source().data('raw'), e.target().data('raw'));
});
cy.on('tap', ev => { if (ev.target === cy && current) closePanel(); });

const search = $('g-search'), suggest = $('g-suggest');
let hits = [], active = -1;
function showSuggest(){
  const q = search.value.trim().toLowerCase();
  hits = q ? cy.nodes().filter(n => n.data('name').toLowerCase().includes(q))
               .sort((a, b) => b.data('degree') - a.data('degree')).slice(0, 8).map(n => n) : [];
  active = hits.length ? 0 : -1;
  suggest.hidden = !hits.length;
  suggest.innerHTML = hits.map((n, i) =>
    '<li role="option" data-i="' + i + '"' + (i === active ? ' class="on"' : '') + '><img src="/entities/' +
    n.data('raw') + '/avatar" alt=""><span>' + esc(n.data('name')) + '</span><small>' +
    n.data('degree') + '</small></li>').join('');
}
function pick(i){
  const n = hits[i];
  suggest.hidden = true;
  if (!n) return;
  search.value = n.data('name');
  focusOn(n.data('raw'));
}
search.addEventListener('input', showSuggest);
search.addEventListener('keydown', ev => {
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
    ev.preventDefault();
    active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + hits.length) % Math.max(hits.length, 1);
    suggest.querySelectorAll('li').forEach((li, i) => li.classList.toggle('on', i === active));
  } else if (ev.key === 'Escape'){ suggest.hidden = true; search.blur(); }
});
suggest.addEventListener('mousedown', ev => {
  const li = ev.target.closest('li');
  if (li){ ev.preventDefault(); pick(Number(li.dataset.i)); }
});
$('g-searchform').addEventListener('submit', ev => {
  ev.preventDefault();
  if (hits.length){ pick(Math.max(active, 0)); return; }
  const q = search.value.trim();
  if (!q) return;
  info.textContent = 'Ищу «' + q + '»…';
  load({q: q, predicate: $('g-pred').value, limit: 400}).then(data => {
    if (data && data.focus_id) openPanel(data.focus_id);
    else info.textContent = 'По запросу «' + q + '» никого не нашли.';
  });
});
document.addEventListener('click', ev => { if (!ev.target.closest('.g-search')) suggest.hidden = true; });

$('g-zoom-in').onclick = () => cy.animate({zoom: cy.zoom() * 1.35, center: {eles: cy.elements()}}, {duration: 200});
$('g-zoom-out').onclick = () => cy.animate({zoom: cy.zoom() / 1.35}, {duration: 200});
$('g-fit').onclick = () => cy.animate({fit: {eles: cy.elements(), padding: 40}}, {duration: 350});
$('g-reset').onclick = () => { closePanel(); load(coreParams()); };
$('g-mindeg').onchange = () => load(coreParams());
$('g-pred').onchange = () => load(coreParams());
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  const menu = document.querySelector('.g-menu');
  if (menu && menu.open) menu.open = false;
  else if (current) closePanel();
});
// Меню фильтров и легенда — всплывающие: закрываются кликом мимо.
document.addEventListener('click', ev => {
  const menu = document.querySelector('.g-menu');
  if (menu && menu.open && !ev.target.closest('.g-menu')) menu.open = false;
  if (!legend.hidden && !ev.target.closest('.g-legend-wrap')) legend.hidden = true;
});
$('g-legend-toggle').onclick = () => {
  legend.hidden = !legend.hidden;
  $('g-legend-toggle').setAttribute('aria-expanded', String(!legend.hidden));
};

const wanted = /person=(\d+)/.exec(location.hash);
if (wanted) load(focusParams(wanted[1])).then(() => openPanel(wanted[1]));
else load(coreParams());
"""
