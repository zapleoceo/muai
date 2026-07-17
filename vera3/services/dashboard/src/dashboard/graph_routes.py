"""Knowledge-graph visualizer — `/graph` page + `/api/graph` JSON.

Renders Vera's L1 substrate (entities + relationships) as an interactive
force-directed graph via Cytoscape.js (loaded from CDN, same pattern as
htmx/telegram-widget elsewhere in the dashboard). The whole graph is 8k+
entities / 7k edges — far too much to draw at once — so the page shows the
*connected core* (degree filter) by default and lets you tap a node to
drill into its ego network. DB access stays in vera_shared.graph.repo.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from vera_shared.graph.clusters import get_clusters, recompute_clusters
from vera_shared.graph.repo import find_entity_by_name, graph_snapshot

from dashboard.render import _render, owner_or_auth_error, owner_or_blank_401

log = logging.getLogger(__name__)
router = APIRouter()

_recluster: dict = {"running": False}

# Stable predicate set (see vera_shared.graph.rel_extract.PREDICATES) — used
# only to build the filter dropdown; the API accepts any string. member_of —
# синтетический предикат membership-рёбер (кто в какой группе).
_PREDICATES = [
    "member_of",
    "coworker_of", "works_at", "friend_of", "client_of", "reports_to",
    "boss_of", "vendor_of", "spouse_of", "parent_of", "child_of",
    "co_founder_of", "lives_in",
]


@router.get("/api/graph", response_class=JSONResponse)
async def graph_data(
    request: Request,
    min_degree: int = Query(2, ge=1, le=50),
    limit: int = Query(300, ge=1, le=800),
    predicate: str | None = None,
    focus: int | None = None,
    q: str | None = None,
):
    if (resp := owner_or_blank_401(request)) is not None:
        return resp
    focus_id = focus
    if focus_id is None and q and q.strip():
        focus_id = await find_entity_by_name(q.strip())
    snap = await graph_snapshot(
        min_degree=min_degree, limit=limit,
        predicate=predicate or None, focus_id=focus_id,
    )
    snap["focus_id"] = focus_id

    # Тематические сообщества Веры (пересчитываются кнопкой, кэш в app_control)
    clusters = await get_clusters()
    if clusters:
        assign = clusters.get("assign", {})
        for n in snap["nodes"]:
            n["cluster"] = assign.get(str(n["id"]))
        snap["cluster_labels"] = clusters.get("labels", {})
        snap["clusters_at"] = clusters.get("computed_at")
    snap["recluster_running"] = _recluster["running"]
    return JSONResponse(snap)


async def _recluster_bg() -> None:
    try:
        await recompute_clusters()
    except Exception as e:
        log.exception("graph recluster failed: %s", e)
    finally:
        _recluster["running"] = False


@router.post("/graph/recluster")
async def graph_recluster(request: Request):
    """Кнопка «Кластеры (Вера)»: label-propagation по связям + LLM-ярлыки.
    Работает в фоне — страница просто перезагружается и подтянет результат."""
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    if not _recluster["running"]:
        _recluster["running"] = True
        asyncio.create_task(_recluster_bg())
    return RedirectResponse("/graph", status_code=303)


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    if (resp := owner_or_auth_error(request)) is not None:
        return resp
    pred_opts = "".join(f'<option value="{p}">{p}</option>' for p in _PREDICATES)
    body = _GRAPH_BODY.replace("__PRED_OPTS__", pred_opts)
    return HTMLResponse(_render("graph", body))


# The page body: control bar + Cytoscape canvas + init script. Kept as a
# plain string (not an f-string) so the JS braces don't need escaping;
# only __PRED_OPTS__ is substituted server-side.
_GRAPH_BODY = """
<h2>🧠 Граф мозга Веры</h2>
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
  <input id="g-search" type="text" placeholder="Найти сущность по имени…"
         style="flex:1;min-width:220px;max-width:340px;padding:10px">
  <label class="mute">связей ≥
    <select id="g-mindeg">
      <option value="1">1</option><option value="2" selected>2</option>
      <option value="3">3</option><option value="5">5</option>
      <option value="10">10</option>
    </select>
  </label>
  <label class="mute">тип связи
    <select id="g-pred"><option value="">любой</option>__PRED_OPTS__</select>
  </label>
  <button id="g-reset">↺ весь граф</button>
  <form method="post" action="/graph/recluster" style="display:inline">
    <button title="Вера сгруппирует граф по темам (команда, семья, чаты…) и подпишет кластеры">
      🎨 Кластеры (Вера)</button>
  </form>
  <span id="g-count" class="mute"></span>
</div>
<div id="g-legend" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px"></div>
<div id="cy" style="height:72vh;background:radial-gradient(ellipse at 50% 40%, #141821 0%, #0c0e13 100%);
     border:1px solid #2a2d34;border-radius:12px"></div>
<div id="g-info" class="mute" style="margin-top:10px;min-height:20px"></div>

<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script>
const TYPE_COLOR = {person:'#4dabf7', group:'#ffc864',
                    supergroup:'#ffc864', channel:'#6dd687'};
// Палитра тематических кластеров Веры (по индексу сообщества).
const CLUSTER_COLORS = ['#4dabf7','#69db7c','#f783ac','#ffa94d','#9775fa',
                        '#3bc9db','#ffd43b','#ff8787','#63e6be','#b197fc',
                        '#e599f7','#a9e34b'];
function clusterColor(c){
  return (c === null || c === undefined) ? null
       : CLUSTER_COLORS[c % CLUSTER_COLORS.length];
}
const cy = cytoscape({
  container: document.getElementById('cy'),
  wheelSensitivity: 0.2,
  style: [
    {selector:'node', style:{
      'background-color': ele => clusterColor(ele.data('cluster'))
                              || TYPE_COLOR[ele.data('type')] || '#8899aa',
      'background-image': ele => '/entities/' + ele.data('raw') + '/avatar',
      'background-fit':'cover', 'background-clip':'node',
      'label':'data(name)', 'color':'#cfd6e0', 'font-size':'9px',
      'text-wrap':'ellipsis', 'text-max-width':'90px',
      'text-margin-y':'-3px',
      'width': ele => 10 + Math.min(44, Math.sqrt(ele.data('degree')||1)*4),
      'height': ele => 10 + Math.min(44, Math.sqrt(ele.data('degree')||1)*4),
      // Кластерный цвет — ободок (заливку закрывает аватарка).
      'border-width': ele => ele.data('cluster')!==null && ele.data('cluster')!==undefined ? 3 : 1,
      'border-color': ele => clusterColor(ele.data('cluster')) || '#2a2d34'}},
    {selector:'node:selected', style:{
      'border-width':4, 'border-color':'#fff', 'font-size':'12px', 'color':'#fff'}},
    {selector:'edge', style:{
      'width': ele => 0.6 + (ele.data('confidence')||0.5)*1.4,
      'line-color':'#3a3f4a', 'curve-style':'haystack', 'opacity':0.5}},
    // Членство в группе — структурная связь, рисуем тоньше и пунктиром,
    // чтобы LLM-факты (works_at и т.п.) выделялись на её фоне.
    {selector:'edge[predicate = "member_of"]', style:{
      'line-style':'dashed', 'width':0.5, 'line-color':'#2f3542', 'opacity':0.35,
      'curve-style':'straight'}},
    {selector:'edge:selected', style:{'line-color':'#4dabf7','opacity':1,'width':2}},
  ],
});

// Имена сущностей и ярлыки кластеров приходят из внешних данных (Telegram,
// Gmail, LLM) — экранируем всё, что попадает в innerHTML.
function esc(s){
  return String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',
    '"':'&quot;',"'":'&#39;'}[ch]));
}
const info = document.getElementById('g-info');
const count = document.getElementById('g-count');
const legend = document.getElementById('g-legend');

function renderLegend(data){
  legend.innerHTML = '';
  const labels = data.cluster_labels || {};
  const present = new Set(data.nodes.map(n=>n.cluster).filter(c=>c!==null&&c!==undefined));
  const items = Object.keys(labels).map(Number).filter(c=>present.has(c)).sort((a,b)=>a-b);
  if (!items.length){
    if (data.recluster_running)
      legend.innerHTML = '<span class="pill warn">⏳ Вера размечает кластеры — обнови страницу через минуту</span>';
    else if (!data.clusters_at)
      legend.innerHTML = '<span class="mute" style="font-size:12px">Нажми «🎨 Кластеры (Вера)» — она сгруппирует граф по темам и подпишет их.</span>';
    return;
  }
  for (const c of items){
    legend.insertAdjacentHTML('beforeend',
      '<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;'+
      'background:#1a1d24;border:1px solid #2a2d34;border-radius:999px;padding:3px 10px">'+
      '<span style="width:10px;height:10px;border-radius:50%;background:'+clusterColor(c)+'"></span>'+
      esc(labels[c])+'</span>');
  }
}

function render(data){
  const els = [];
  for (const n of data.nodes)
    els.push({data:{id:'n'+n.id, name:n.name, type:n.type, degree:n.degree,
                     raw:n.id, username:n.username, tg_id:n.tg_id,
                     cluster:(n.cluster===undefined?null:n.cluster)}});
  const seen = new Set(data.nodes.map(n=>'n'+n.id));
  for (const e of data.edges){
    const s='n'+e.source, t='n'+e.target;
    if (seen.has(s) && seen.has(t))
      els.push({data:{id:s+'_'+t+'_'+e.predicate, source:s, target:t,
                       predicate:e.predicate, confidence:e.confidence}});
  }
  cy.elements().remove();
  cy.add(els);
  cy.layout({name:'cose', animate:false, nodeRepulsion:9000,
             idealEdgeLength:70, nodeOverlap:10, gravity:60}).run();
  renderLegend(data);
  count.textContent = data.nodes.length + ' сущностей, ' + data.edges.length + ' связей';
  if (data.focus_id){
    const f = data.nodes.find(n => n.id === data.focus_id);
    const link = f ? tgLink(f.username, f.tg_id) : null;
    const linkHtml = link
      ? ' · <a href="' + esc(link) + '"' + (link.indexOf('http')===0?' target="_blank" rel="noopener"':'') +
        '>открыть в Telegram' + (f.username ? ' (@' + esc(f.username) + ')' : '') + '</a>'
      : '';
    info.innerHTML = 'Фокус на «' + esc(f?f.name:('#'+data.focus_id)) + '» и её соседях' +
                     linkHtml + '. «↺ весь граф» — назад.';
    return;
  }
  info.textContent = data.focus_id
    ? 'Фокус на сущности #' + data.focus_id + ' и её соседях. «↺ весь граф» — назад.'
    : 'Ядро графа (самые связанные). Клик по узлу — раскрыть его окружение.';
}

function load(params){
  const qs = new URLSearchParams(params).toString();
  fetch('/api/graph?' + qs, {credentials:'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(render)
    .catch(err => { info.textContent = 'Ошибка загрузки графа: ' + err; });
}

function coreParams(){
  return {min_degree: document.getElementById('g-mindeg').value,
          predicate: document.getElementById('g-pred').value, limit: 300};
}

function tgLink(u, id){
  if (u) return 'https://t.me/' + String(u).replace(/^@/, '');
  if (id) return 'tg://user?id=' + id;
  return null;
}
cy.on('tap', 'node', ev => {
  // Persistent link + focus info is rendered by render() once the ego network
  // loads (it has the focus node's username/tg_id); here just instant feedback.
  info.textContent = 'Загружаю окружение «' + ev.target.data('name') + '»…';
  load({focus: ev.target.data('raw'),
        predicate: document.getElementById('g-pred').value, limit: 400});
});
document.getElementById('g-reset').onclick = () => load(coreParams());
document.getElementById('g-mindeg').onchange = () => load(coreParams());
document.getElementById('g-pred').onchange = () => load(coreParams());
document.getElementById('g-search').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.value.trim()){
    info.textContent = 'Ищу «' + e.target.value.trim() + '»…';
    load({q: e.target.value.trim(),
          predicate: document.getElementById('g-pred').value, limit: 400});
  }
});

load(coreParams());
</script>
"""
