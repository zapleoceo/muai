"""Разметка страницы «Люди» (`/graph`): поиск, фильтры под «Фильтры», холст
Cytoscape и боковая панель. Скрипт — в `graph_script`.

Плейсхолдеры `__PRED_OPTS__`, `__PRED_LABELS__`, `__DUP_LABEL__` подставляет
`graph_body`: тело — обычная строка, а не f-string, чтобы не экранировать
скобки CSS и JS.
"""
from __future__ import annotations

from dashboard.graph_labels import predicate_labels_json, predicate_options_html
from dashboard.graph_script import GRAPH_SCRIPT

GRAPH_CSS = """
.g-head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.g-head h2{margin:0 0 .6rem}
.g-search{display:flex;gap:.6rem;margin:0 0 .6rem}
.g-search input{margin:0;flex:1;min-width:0}.g-search button{margin:0;width:auto}
.g-filters{margin:0 0 .6rem}
.g-filter-row{display:flex;flex-wrap:wrap;gap:.8rem;align-items:end;padding-top:.6rem}
.g-filter-row label,.g-filter-row select,.g-filter-row button,.g-filter-row form{margin:0}
.g-legend{display:flex;gap:.5rem;flex-wrap:wrap;margin:.4rem 0}
.g-legend .chip{display:inline-flex;align-items:center;gap:.35rem}
.swatch{width:.65rem;height:.65rem;border-radius:50%;display:inline-block}
.g-layout{display:flex;gap:1rem;align-items:stretch}
.g-canvas{flex:1;min-width:0;height:72vh;background:var(--vera-surface);border:1px solid var(--vera-line);
border-radius:var(--pico-border-radius)}
.g-panel{width:22rem;max-height:72vh;overflow:auto;background:var(--vera-surface);
border:1px solid var(--vera-line);border-radius:var(--pico-border-radius);padding:1rem}
.g-panel[hidden]{display:none}
.g-panel h3{margin:0;font-size:1.15rem;overflow-wrap:anywhere}
.g-panel h4{margin:1rem 0 .3rem;font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--vera-muted)}
.g-panel ul{list-style:none;padding:0;margin:0}.g-panel li{margin:0 0 .45rem;padding:0;overflow-wrap:anywhere}
.g-panel li a{display:inline;padding:0;margin:0}
.g-panel-head{display:flex;gap:.7rem;align-items:center}
.g-panel-head img{width:2.6rem;height:2.6rem;border-radius:50%;flex:none}
.g-panel-head .g-close{margin-left:auto;width:auto;padding:.1rem .6rem;margin-bottom:0}
.g-chips{display:flex;gap:.35rem;flex-wrap:wrap;margin:.5rem 0}
.g-panel [role=button]{margin-top:1rem;width:100%}
@media (max-width:760px){.g-layout{flex-direction:column}.g-canvas{flex:none;width:100%;height:55vh}
.g-panel{width:auto;max-height:none}}
"""

_BODY = """
<style>__CSS__</style>
<div class="g-head">
  <h2>Люди</h2>
  <a class="chip" id="g-dupes" href="/entities/duplicates">__DUP_LABEL__</a>
</div>
<form id="g-searchform" class="g-search" role="search">
  <input id="g-search" type="search" placeholder="Найти человека или чат по имени…"
         autocomplete="off">
  <button type="submit">Найти</button>
</form>
<details class="g-filters"><summary>Фильтры</summary>
  <div class="g-filter-row">
    <label>связей ≥
      <select id="g-mindeg">
        <option value="1">1</option><option value="2" selected>2</option>
        <option value="3">3</option><option value="5">5</option>
        <option value="10">10</option>
      </select>
    </label>
    <label>тип связи
      <select id="g-pred"><option value="">любой</option>__PRED_OPTS__</select>
    </label>
    <button type="button" id="g-reset" class="secondary">↺ весь граф</button>
    <form method="post" action="/graph/recluster">
      <button class="secondary" title="Вера разобьёт граф на темы (работа, друзья, чаты…) и покрасит узлы по темам. Данные не меняются.">
        Раскрасить по темам</button>
    </form>
  </div>
</details>
<div id="g-legend" class="g-legend"></div>
<div class="g-layout">
  <div id="cy" class="g-canvas"></div>
  <aside id="g-panel" class="g-panel" hidden aria-live="polite"></aside>
</div>
<div id="g-info" class="muted small"></div>
<span id="g-count" class="muted small"></span>
<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script>__SCRIPT__</script>
"""


def dupes_label(pending: int | None) -> str:
    return "Дубли" if pending is None else f"Дубли ({pending})"


def graph_body(predicates: list[str], pending: int | None) -> str:
    return (_BODY.replace("__CSS__", GRAPH_CSS)
            .replace("__DUP_LABEL__", dupes_label(pending))
            .replace("__PRED_OPTS__", predicate_options_html(predicates))
            .replace("__SCRIPT__", GRAPH_SCRIPT)
            .replace("__PRED_LABELS__", predicate_labels_json(predicates)))
