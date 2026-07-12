"""Knowledge-graph visualizer — `/graph` page + `/api/graph` JSON.

Renders Vera's L1 substrate (entities + relationships) as an interactive
force-directed graph via Cytoscape.js (loaded from CDN, same pattern as
htmx/telegram-widget elsewhere in the dashboard). The whole graph is 8k+
entities / 7k edges — far too much to draw at once — so the page shows the
*connected core* (degree filter) by default and lets you tap a node to
drill into its ego network. DB access stays in vera_shared.graph.repo.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from vera_shared.graph.repo import find_entity_by_name, graph_snapshot

from dashboard.render import _render, owner_or_auth_error, owner_or_blank_401

router = APIRouter()

# Stable predicate set (see vera_shared.graph.rel_extract.PREDICATES) — used
# only to build the filter dropdown; the API accepts any string.
_PREDICATES = [
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
    return JSONResponse(snap)


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
  <span id="g-count" class="mute"></span>
</div>
<div id="cy" style="height:74vh;background:#0f1115;border:1px solid #2a2d34;
     border-radius:12px"></div>
<div id="g-info" class="mute" style="margin-top:10px;min-height:20px"></div>

<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script>
const TYPE_COLOR = {person:'#4dabf7', group:'#ffc864',
                    supergroup:'#ffc864', channel:'#6dd687'};
const cy = cytoscape({
  container: document.getElementById('cy'),
  wheelSensitivity: 0.2,
  style: [
    {selector:'node', style:{
      'background-color': ele => TYPE_COLOR[ele.data('type')] || '#8899aa',
      'background-image': ele => '/entities/' + ele.data('raw') + '/avatar',
      'background-fit':'cover', 'background-clip':'node',
      'label':'data(name)', 'color':'#cfd6e0', 'font-size':'9px',
      'text-wrap':'ellipsis', 'text-max-width':'90px',
      'width': ele => 8 + Math.min(40, Math.sqrt(ele.data('degree')||1)*4),
      'height': ele => 8 + Math.min(40, Math.sqrt(ele.data('degree')||1)*4),
      'border-width':0.5, 'border-color':'#0f1115'}},
    {selector:'node:selected', style:{
      'border-width':2, 'border-color':'#fff', 'font-size':'12px', 'color':'#fff'}},
    {selector:'edge', style:{
      'width':0.8, 'line-color':'#3a3f4a', 'curve-style':'haystack',
      'opacity':0.55}},
    {selector:'edge:selected', style:{'line-color':'#4dabf7','opacity':1,'width':1.6}},
  ],
});

const info = document.getElementById('g-info');
const count = document.getElementById('g-count');

function render(data){
  const els = [];
  for (const n of data.nodes)
    els.push({data:{id:'n'+n.id, name:n.name, type:n.type, degree:n.degree,
                     raw:n.id, username:n.username, tg_id:n.tg_id}});
  const seen = new Set(data.nodes.map(n=>'n'+n.id));
  for (const e of data.edges){
    const s='n'+e.source, t='n'+e.target;
    if (seen.has(s) && seen.has(t))
      els.push({data:{id:s+'_'+t+'_'+e.predicate, source:s, target:t,
                       predicate:e.predicate, confidence:e.confidence}});
  }
  cy.elements().remove();
  cy.add(els);
  cy.layout({name:'cose', animate:false, nodeRepulsion:8000,
             idealEdgeLength:60, nodeOverlap:8}).run();
  count.textContent = data.nodes.length + ' сущностей, ' + data.edges.length + ' связей';
  if (data.focus_id){
    const f = data.nodes.find(n => n.id === data.focus_id);
    const link = f ? tgLink(f.username, f.tg_id) : null;
    const linkHtml = link
      ? ' · <a href="' + link + '"' + (link.indexOf('http')===0?' target="_blank" rel="noopener"':'') +
        '>открыть в Telegram' + (f.username ? ' (@' + f.username + ')' : '') + '</a>'
      : '';
    info.innerHTML = 'Фокус на «' + (f?f.name:('#'+data.focus_id)) + '» и её соседях' +
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
