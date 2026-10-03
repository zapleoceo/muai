"""Разметка страницы «Люди» (`/graph`): холст Cytoscape, поверх него поиск,
фильтры, легенда, зум и карточка человека. Стили — `graph_css`, скрипт —
`graph_script`.

Плейсхолдеры `__PRED_OPTS__`, `__PRED_LABELS__`, `__DUP_LABEL__` подставляет
`graph_body`: тело — обычная строка, а не f-string, чтобы не экранировать
скобки CSS и JS.
"""
from __future__ import annotations

from dashboard.graph_css import GRAPH_CSS
from dashboard.graph_labels import predicate_labels_json, predicate_options_html
from dashboard.graph_script import GRAPH_SCRIPT
from dashboard.ui.theme import CYTOSCAPE_SRI, CYTOSCAPE_URL

_BODY = """
<style>__CSS__</style>
<div class="page-head g-head">
  <div><h1>Люди</h1><p id="g-info" class="muted" aria-live="polite"></p></div>
  <div class="g-head-actions">
    <a class="chip" id="g-dupes" href="/entities/duplicates">__DUP_LABEL__</a>
    <a class="chip" href="/journal">Журнал правок</a>
  </div>
</div>
<div class="g-stage">
  <div id="cy" class="g-canvas" aria-label="Граф людей и связей"></div>
  <div class="g-toolbar">
    <form id="g-searchform" class="g-search" role="search">
      <input id="g-search" type="search" placeholder="Найти человека…  ( / )"
             autocomplete="off" data-hotkey-search aria-controls="g-suggest">
      <ul id="g-suggest" role="listbox" hidden></ul>
    </form>
    <details class="g-menu"><summary>Фильтры</summary>
      <div class="g-menu-body">
        <label>связей не меньше
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
  </div>
  <div id="g-legend" class="g-legend"></div>
  <span id="g-count" class="g-count"></span>
  <div class="g-zoom">
    <button type="button" id="g-zoom-in" class="secondary" aria-label="Приблизить">+</button>
    <button type="button" id="g-zoom-out" class="secondary" aria-label="Отдалить">−</button>
    <button type="button" id="g-fit" class="secondary" aria-label="Показать весь граф">⤢</button>
  </div>
  <aside id="g-panel" class="g-panel" hidden aria-live="polite"></aside>
</div>
<script src="__CY_URL__" integrity="__CY_SRI__" crossorigin="anonymous"></script>
<script>__SCRIPT__</script>
"""


def dupes_label(pending: int | None) -> str:
    return "Дубли" if pending is None else f"Дубли ({pending})"


def graph_body(predicates: list[str], pending: int | None) -> str:
    return (_BODY.replace("__CSS__", GRAPH_CSS)
            .replace("__DUP_LABEL__", dupes_label(pending))
            .replace("__PRED_OPTS__", predicate_options_html(predicates))
            .replace("__CY_URL__", CYTOSCAPE_URL).replace("__CY_SRI__", CYTOSCAPE_SRI)
            .replace("__SCRIPT__", GRAPH_SCRIPT)
            .replace("__PRED_LABELS__", predicate_labels_json([*predicates, "contact"])))
