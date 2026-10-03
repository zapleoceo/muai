"""Тема дашборда: Pico CSS с закреплённой версией плюс один небольшой слой
`VERA_CSS` — токены, привязанные к `--pico-*`, и вспомогательные классы.

Слой лежит строкой в Python и вставляется в `<style>` каждой страницы, а не
отдаётся статикой: образ собирается `pip install -e` из `src/`, отдельный
монтируемый каталог и проверка путей ради 3 КБ стилей были бы лишним."""
from __future__ import annotations

PICO_URL = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css"
PICO_SRI = "sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU"
HTMX_URL = "https://unpkg.com/htmx.org@1.9.10/dist/htmx.min.js"
HTMX_SRI = "sha384-D1Kt99CQMDuVetoL1lrYwg5t+9QdHe7NLX/SoJYkXDFfX37iInKRy5xLSi8nO7UC"

VERA_CSS = """
:root{--vera-ok:#6dd687;--vera-warn:#ffc864;--vera-err:#ff8a8a;--vera-muted:#8a94a0;
--vera-surface:#1a1d24;--vera-line:#2a2d34}
[data-theme=dark]{--pico-background-color:#0f1115;--pico-color:#e4e6eb;
--pico-card-background-color:var(--vera-surface);--pico-card-sectioning-background-color:var(--vera-surface);
--pico-primary:#4dabf7;--pico-primary-background:#2f7fc1;--pico-primary-hover-background:#3a9ce0;
--pico-muted-color:var(--vera-muted);--pico-muted-border-color:var(--vera-line);
--pico-form-element-background-color:#0f1115;--pico-form-element-border-color:var(--vera-line);
--pico-del-color:var(--vera-err);--pico-ins-color:var(--vera-ok)}
body>main{padding-block:1rem}
nav.top{margin-bottom:1.2rem;border-bottom:1px solid var(--vera-line)}
nav.top a[aria-current=page]{font-weight:600;color:var(--pico-contrast)}
nav.top .out{color:var(--vera-muted)}
.muted,.mute{color:var(--vera-muted)}
.small{font-size:.85rem}
.dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;background:var(--vera-muted);
vertical-align:middle;margin-right:.35rem}
.dot.ok{background:var(--vera-ok)}.dot.warn{background:var(--vera-warn)}.dot.err{background:var(--vera-err)}
.chip,.pill{display:inline-block;padding:.1rem .6rem;border:1px solid var(--vera-line);
border-radius:999px;font-size:.8rem;line-height:1.5;color:var(--pico-color)}
a.chip{text-decoration:none}a.chip.on{border-color:var(--pico-primary);color:var(--pico-primary)}
.pill.ok{background:#14422c;color:var(--vera-ok);border:0}
.pill.warn{background:#4a3a14;color:var(--vera-warn);border:0}
.pill.err{background:#4a1a1d;color:var(--vera-err);border:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:1rem;margin:1rem 0}
.stat,.card{background:var(--vera-surface);border:1px solid var(--vera-line);border-radius:var(--pico-border-radius);padding:1rem}
.stat .k,.card-label{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--vera-muted)}
.stat .v,.card-value{font-size:1.6rem;font-weight:600;font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:1rem;margin:0 0 1.5rem}
.card-value small,.card-sub{font-size:.8rem;color:var(--vera-muted);font-weight:400}
.section{background:var(--vera-surface);border-radius:var(--pico-border-radius);padding:1.2rem;margin:1rem 0}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.5rem 0;border-bottom:1px solid var(--vera-line)}
.row:last-child{border-bottom:0}
button.danger,a.danger[role=button],.danger{--pico-background-color:#5a2226;--pico-border-color:#5a2226;
--pico-color:#ffd0d0}
a.danger:not([role=button]){color:var(--vera-err)}
table.data{font-size:.85rem}
table.data th{font-size:.72rem;text-transform:uppercase;color:var(--vera-muted)}
.preview{color:#ccc;max-width:38rem;overflow:hidden;text-overflow:ellipsis}
.answer{background:var(--pico-form-element-background-color);padding:1rem 1.2rem;border-radius:var(--pico-border-radius);
border:1px solid var(--vera-line);line-height:1.6;margin:1rem 0}
.answer ul{margin:.5rem 0}.answer li{margin-bottom:.2rem}.answer code{font-size:.85em}
ul.sources{padding-left:1.1rem}ul.sources li{margin-bottom:.6rem}
.meta{color:var(--vera-muted);font-size:.8rem}
.error,.err{color:var(--vera-err)}
.error{background:#4a1a1d;padding:.8rem 1rem;border-radius:var(--pico-border-radius)}
.htmx-indicator{display:none}.htmx-request .htmx-indicator,.htmx-request.htmx-indicator{display:inline}
.status-line{display:block;margin:0 0 1rem;font-size:.9rem;color:var(--vera-muted);text-decoration:none}
.ask input[type=text]{font-size:1.15rem;padding:1rem}
.day{margin:1.4rem 0 .3rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--vera-muted)}
.narrow{max-width:34rem;margin:8vh auto}
details>summary{cursor:pointer}
"""

SOURCES_CSS = """
.src-list { width:100%; border-collapse:collapse; font-size:14px; }
.src-list th { font-size:11px; text-transform:uppercase; color:var(--vera-muted); font-weight:500;
               text-align:left; padding:0 12px 8px 0; white-space:nowrap; }
.src-list td { padding:12px 12px 12px 0; background:none; border-top:1px solid var(--vera-line);
               vertical-align:middle; }
.src-list tr:hover td { background:var(--pico-card-background-color); }
.src-name { display:flex; align-items:center; gap:10px; }
.src-name .ico { font-size:17px; width:22px; text-align:center; }
.src-name a { font-weight:600; }
.src-how { color:var(--vera-muted); font-size:12px; margin-top:2px; }
.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.act { text-align:right; white-space:nowrap; }
.act a { font-size:12px; padding:5px 11px; border:1px solid var(--vera-line); border-radius:7px;
         color:var(--vera-muted); }
.act a:hover { border-color:var(--pico-primary); color:var(--pico-primary); }
.act a.danger:hover, a.btn.danger:hover { border-color:var(--vera-err); color:var(--vera-err); }
a.btn { padding:8px 16px; border:1px solid var(--vera-line); border-radius:8px;
        color:var(--vera-muted); font-size:13px; }
a.btn:hover { border-color:var(--pico-primary); color:var(--pico-primary); }
.idle td { opacity:.55; }
.crumb { font-size:13px; color:var(--vera-muted); margin:0 0 10px; }
.head { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin:0 0 4px; }
.head h1 { margin:0; font-size:24px; }
.strip { display:flex; gap:28px; flex-wrap:wrap; margin:18px 0 4px;
         padding:16px 0; border-top:1px solid var(--vera-line); border-bottom:1px solid var(--vera-line); }
.strip div { min-width:110px; }
.strip .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--vera-muted); }
.strip .v { font-size:22px; font-weight:600; margin-top:3px;
            font-variant-numeric:tabular-nums; }
.blocks { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
          gap:18px; margin-top:22px; }
.blk { background:var(--vera-surface); border:1px solid var(--vera-line); border-radius:12px; padding:16px 18px; }
.blk.wide { grid-column:1/-1; }
.blk h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
          color:var(--vera-muted); margin:0 0 12px; }
.blk .hint { color:var(--vera-muted); font-size:12px; margin-top:12px; line-height:1.45; }
.note { color:var(--vera-muted); font-size:13px; margin:6px 0 0; }
"""
