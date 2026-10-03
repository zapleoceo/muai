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
--vera-surface:#1a1d24;--vera-line:#2a2d34;--vera-ok-bg:#14422c;--vera-warn-bg:#4a3a14;--vera-err-bg:#4a1a1d;--vera-on-err:#2b0a0c}
[data-theme=dark]{--pico-background-color:#0f1115;--pico-color:#e4e6eb;
--pico-card-background-color:var(--vera-surface);--pico-card-sectioning-background-color:var(--vera-surface);
--pico-primary:#4dabf7;--pico-primary-background:#2f7fc1;--pico-primary-hover-background:#3a9ce0;
--pico-muted-color:var(--vera-muted);--pico-muted-border-color:var(--vera-line);
--pico-form-element-background-color:#0f1115;--pico-form-element-border-color:var(--vera-line);
--pico-del-color:var(--vera-err);--pico-ins-color:var(--vera-ok)}
body>main{padding-block:1rem}
nav.top{margin-bottom:1.2rem;border-bottom:1px solid var(--vera-line);flex-wrap:wrap;column-gap:1rem}
nav.top ul{flex-wrap:wrap}
@media (max-width:640px){nav.top{--pico-nav-element-spacing-vertical:.3rem;--pico-nav-element-spacing-horizontal:.5rem}
}
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
.pill.ok{background:var(--vera-ok-bg);color:var(--vera-ok);border:0}
.pill.warn{background:var(--vera-warn-bg);color:var(--vera-warn);border:0}
.pill.err{background:var(--vera-err-bg);color:var(--vera-err);border:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:1rem;margin:1rem 0}
.stat,.card{background:var(--vera-surface);border:1px solid var(--vera-line);border-radius:var(--pico-border-radius);padding:1rem}
.stat .k,.card-label{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--vera-muted)}
.stat .v,.card-value{font-size:1.6rem;font-weight:600;font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:1rem;margin:0 0 1.5rem}
.card-value small,.card-sub{font-size:.8rem;color:var(--vera-muted);font-weight:400}
.section{background:var(--vera-surface);border-radius:var(--pico-border-radius);padding:1.2rem;margin:1rem 0}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.5rem 0;border-bottom:1px solid var(--vera-line)}
.row:last-child{border-bottom:0}
.pill.off{background:transparent;border:1px dashed var(--vera-line);color:var(--vera-muted)}
button.danger,a.danger{background:transparent;border:1px solid var(--vera-err);color:var(--vera-err)}
button.danger:hover,a.danger:hover{background:var(--vera-err-bg)}
button.danger-solid,a.danger-solid{background:var(--vera-err);border:1px solid var(--vera-err);color:var(--vera-on-err)}
button.danger-solid:hover{background:var(--vera-err);filter:brightness(1.12)}
table.data{font-size:.85rem}
table.data th{font-size:.72rem;text-transform:uppercase;color:var(--vera-muted)}
td.preview{max-width:0;width:100%;min-width:12rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.preview a{color:inherit;text-decoration:none}
.who-cell{min-width:9rem;max-width:14rem}
.who-cell div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nowrap{white-space:nowrap}
tr.row-link{cursor:pointer}tr.row-link:hover td{background:var(--vera-surface)}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:.35rem 1.4rem;margin:1rem 0}
.kv dt{color:var(--vera-muted)}.kv dd{margin:0;overflow-wrap:anywhere}
.body-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55;background:var(--vera-surface);
border:1px solid var(--vera-line);border-radius:var(--pico-border-radius);padding:1rem 1.2rem}
.crumb{font-size:.85rem;margin:0 0 .6rem}
.tz-note{margin-top:2rem;font-size:.7rem;text-align:center;color:var(--vera-muted)}
.answer{background:var(--pico-form-element-background-color);padding:1rem 1.2rem;border-radius:var(--pico-border-radius);
border:1px solid var(--vera-line);line-height:1.6;margin:1rem 0}
.answer ul{margin:.5rem 0}.answer li{margin-bottom:.2rem}.answer code{font-size:.85em}
ul.sources{padding-left:1.1rem}ul.sources li{margin-bottom:.6rem}
.meta{color:var(--vera-muted);font-size:.8rem}
.error,.err{color:var(--vera-err)}
.error{background:var(--vera-err-bg);padding:.8rem 1rem;border-radius:var(--pico-border-radius)}
.htmx-indicator{display:none}.htmx-request .htmx-indicator,.htmx-request.htmx-indicator{display:inline}
.status-line{display:block;margin:0 0 1rem;font-size:.9rem;color:var(--vera-muted);text-decoration:none}
.ask input[type=text]{font-size:1.15rem;padding:1rem}
.day{margin:1.4rem 0 .3rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--vera-muted)}
.narrow{max-width:34rem;margin:8vh auto}
details>summary{cursor:pointer}
form.inline{margin:0;display:inline-flex;gap:.6rem;align-items:center}
form.inline button{margin:0;width:auto}
"""

DUPES_CSS = """
.pair{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:.6rem 0}
.cand-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr));gap:1rem;margin:.6rem 0}
.pair-card{background:var(--vera-surface);border:1px solid var(--vera-line);
border-radius:var(--pico-border-radius);padding:1rem;margin:0 0 1rem}
.pair-card h4{margin:0 0 .2rem;font-size:1rem}.pair-card p{margin:0 0 .4rem}
.person{border:1px solid var(--vera-line);border-radius:var(--pico-border-radius);padding:.7rem;min-width:0}
.person-head{display:flex;gap:.6rem;align-items:flex-start;overflow-wrap:anywhere}
.person-head img{border-radius:50%;flex:none}
.person .badge{margin-left:auto;font-size:.7rem;color:var(--vera-muted);white-space:nowrap}
.person blockquote{margin:.4rem 0 0;padding:0 0 0 .6rem;border-left:2px solid var(--vera-line);
font-size:.8rem;color:var(--vera-muted)}
.chips{margin:.4rem 0}
.actions,.bulk{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.actions form,.bulk form{margin:0;display:inline-flex;gap:.6rem}
.actions button,.bulk button{margin:0;width:auto}
.select-merge{display:flex;gap:.8rem;flex-wrap:wrap;align-items:end}
.select-merge label,.select-merge select,.select-merge button{margin:0}
.warning{border-left:3px solid var(--vera-warn);padding:.5rem .8rem;color:var(--vera-warn)}
.bulk{margin:.6rem 0 1.2rem}
@media (max-width:640px){.pair{grid-template-columns:1fr}
}
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
.act a { background:transparent; font-size:12px; padding:5px 11px; border:1px solid var(--vera-line); border-radius:7px;
         color:var(--vera-muted); }
.act a:hover { border-color:var(--pico-primary); color:var(--pico-primary); }
.act a.danger, a.btn.danger { background:transparent; color:var(--vera-err); border-color:var(--vera-line); }
.act a.danger:hover, a.btn.danger:hover { border-color:var(--vera-err); background:var(--vera-err-bg); }
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
.push-right { margin-left:auto; }
.strip .v.v-small { font-size:15px; }
.note { color:var(--vera-muted); font-size:13px; margin:6px 0 0; }
"""
