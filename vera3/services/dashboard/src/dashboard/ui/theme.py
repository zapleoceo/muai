"""Тема дашборда: один тёмный стиль без внешнего CSS-фреймворка.

Pico CSS убран: он задаёт собственную систему переменных и вид каждого элемента,
из-за чего нам приходилось переопределять половину его правил (кнопки, таблицы,
`details`), а третья часть страницы тянула 80 КБ ради стилей, которые мы не
использовали. Теперь токены и компоненты наши (`css_base`, `css_controls`,
`css_pages`), их ~14 КБ. Стили и скрипт отдаются статикой с хэшем в адресе
(`assets_routes`) и кэшируются навсегда; на странице остаются только htmx и
Cytoscape с закреплёнными версиями и SRI.
"""
from __future__ import annotations

import hashlib

from dashboard.ui.css_base import BASE_CSS, NAV_CSS, TOKENS_CSS
from dashboard.ui.css_controls import CONTROLS_CSS
from dashboard.ui.css_pages import DUPES_CSS, PAGES_CSS, SOURCES_CSS  # noqa: F401
from dashboard.ui.js_core import UI_JS

HTMX_URL = "https://unpkg.com/htmx.org@1.9.10/dist/htmx.min.js"
HTMX_SRI = "sha384-D1Kt99CQMDuVetoL1lrYwg5t+9QdHe7NLX/SoJYkXDFfX37iInKRy5xLSi8nO7UC"
CYTOSCAPE_URL = "https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js"
CYTOSCAPE_SRI = "sha384-IWROdLKRsN1UuJywMlWl7/blXQ8GEooN2n7dzTxfEPd7ybYIKCUJ2Ol/1Gpf3YV4"

VERA_CSS = TOKENS_CSS + BASE_CSS + NAV_CSS + CONTROLS_CSS + PAGES_CSS

# Версия в адресе: смена стилей или скрипта даёт новый URL, а старый кэш не мешает.
ASSET_VERSION = hashlib.sha256((VERA_CSS + UI_JS).encode()).hexdigest()[:10]
CSS_URL = f"/ui/vera.css?v={ASSET_VERSION}"
JS_URL = f"/ui/vera.js?v={ASSET_VERSION}"
