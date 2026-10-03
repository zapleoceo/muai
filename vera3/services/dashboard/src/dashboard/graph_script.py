"""Скрипт страницы «Люди»: части склеиваются в один `<script>` (общая область
видимости), порядок важен только для кода верхнего уровня — он идёт последним."""
from __future__ import annotations

from dashboard.graph_script_connections import CONNECTIONS_SCRIPT
from dashboard.graph_script_core import CORE_SCRIPT
from dashboard.graph_script_merge import MERGE_SCRIPT
from dashboard.graph_script_panel import PANEL_SCRIPT
from dashboard.graph_script_roles import ROLES_SCRIPT
from dashboard.graph_script_ui import UI_SCRIPT

GRAPH_SCRIPT = CORE_SCRIPT + CONNECTIONS_SCRIPT + PANEL_SCRIPT + ROLES_SCRIPT + MERGE_SCRIPT + UI_SCRIPT
