"""Компоненты дизайн-системы: кнопки, поля, чипы, карточки, таблицы, диалог,
тост, скелетон. Разметка страниц использует обычные элементы (`button`,
`input`, `table`, `details`) и несколько классов — стили вешаются на них."""
from __future__ import annotations

CONTROLS_CSS = """
button,a[role=button],.btn,input[type=submit]{display:inline-flex;align-items:center;justify-content:center;gap:.4rem;
font:inherit;font-size:.9rem;font-weight:550;line-height:1.2;padding:.55rem 1rem;margin:0;cursor:pointer;white-space:nowrap;
color:var(--accent-ink);background:linear-gradient(180deg,var(--accent-strong),var(--accent));border:1px solid transparent;
border-radius:var(--r-md);box-shadow:0 1px 0 rgba(255,255,255,.35) inset,0 6px 18px -6px rgba(139,140,255,.6);
transition:transform var(--fast) var(--ease),filter var(--fast),background var(--fast),border-color var(--fast),box-shadow var(--fast)}
button:hover,a[role=button]:hover,.btn:hover{filter:brightness(1.08);color:var(--accent-ink);transform:translateY(-1px)}
button:active,a[role=button]:active{transform:translateY(0) scale(.98)}
button.secondary,a[role=button].secondary,.btn,button.outline,a.btn{color:var(--text);background:var(--surface-2);
border-color:var(--line-strong);box-shadow:var(--shadow-1)}
button.secondary:hover,a[role=button].secondary:hover,.btn:hover,button.outline:hover{color:var(--text-strong);
background:var(--surface-3);filter:none;border-color:var(--accent-line)}
button.ghost{background:transparent;box-shadow:none;border-color:transparent;color:var(--muted)}
button.ghost:hover{color:var(--text-strong);background:rgba(255,255,255,.06);filter:none}
button.danger,a.danger,.btn.danger{color:var(--err);background:transparent;border-color:rgba(255,122,136,.4);box-shadow:none}
button.danger:hover,a.danger:hover,.btn.danger:hover{background:var(--err-bg);border-color:var(--err);color:var(--err);filter:none}
button.danger-solid,a.danger-solid{color:#2b0a0c;background:linear-gradient(180deg,#ff9aa5,var(--err));
box-shadow:0 6px 18px -6px rgba(255,122,136,.6)}
button.danger-solid:hover{color:#2b0a0c}
button.sm{padding:.3rem .65rem;font-size:.8rem;border-radius:var(--r-sm)}
button:disabled,button[aria-busy=true]{opacity:.5;cursor:not-allowed;transform:none}
form{margin:0 0 1rem}
form.inline{display:inline-flex;gap:.6rem;align-items:center;margin:0}
label{display:block;margin:0 0 .35rem;font-size:.85rem;color:var(--muted)}
input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=hidden]),select,textarea{width:100%;font:inherit;font-size:.95rem;
color:var(--text);background:var(--bg-inset);border:1px solid var(--line-strong);border-radius:var(--r-md);padding:.6rem .85rem;margin:0 0 .8rem;
transition:border-color var(--fast),box-shadow var(--fast),background var(--fast)}
input::placeholder,textarea::placeholder{color:var(--faint)}
input:hover,select:hover,textarea:hover{border-color:rgba(255,255,255,.26)}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);outline:none}
select{appearance:none;padding-right:2rem;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
background-position:calc(100% - 17px) 55%,calc(100% - 12px) 55%;background-size:5px 5px;background-repeat:no-repeat}
input[type=checkbox],input[type=radio]{accent-color:var(--accent);width:1rem;height:1rem;vertical-align:-.15em;margin-right:.4rem}
input[type=search]{background-image:none}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:.7rem;align-items:end}
.grid>*{margin:0}
details{border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface);margin:.8rem 0;padding:0 1rem}
details>summary{list-style:none;cursor:pointer;padding:.75rem 0;color:var(--text);font-weight:550;display:flex;align-items:center;gap:.5rem}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"";width:.45rem;height:.45rem;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
transform:rotate(-45deg);transition:transform var(--fast)}
details[open]>summary::before{transform:rotate(45deg)}
details[open]{padding-bottom:1rem}
.chip,.pill{display:inline-flex;align-items:center;gap:.35rem;padding:.12rem .65rem;border:1px solid var(--line-strong);border-radius:var(--r-pill);
font-size:.8rem;line-height:1.5;color:var(--text);background:rgba(255,255,255,.025)}
a.chip{transition:border-color var(--fast),background var(--fast)}
a.chip:hover{border-color:var(--accent-line);background:var(--accent-soft);color:var(--text-strong)}
a.chip.on,.chip.on{border-color:var(--accent-line);color:var(--accent-strong);background:var(--accent-soft)}
.pill.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent}.pill.warn{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.pill.err{background:var(--err-bg);color:var(--err);border-color:transparent}.pill.off{border-style:dashed;color:var(--muted);background:transparent}
.dot{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;background:var(--faint);vertical-align:middle;margin-right:.4rem}
.dot.ok{background:var(--ok);box-shadow:0 0 10px var(--ok)}.dot.warn{background:var(--warn);box-shadow:0 0 10px var(--warn)}
.dot.err{background:var(--err);box-shadow:0 0 10px var(--err)}
.stat,.card,article,.section,.pair-card,.blk{background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(255,255,255,.012)),var(--surface);
border:1px solid var(--line);border-radius:var(--r-lg);padding:1.1rem 1.25rem;box-shadow:var(--shadow-1)}
article{margin:1rem 0}.section{margin:1rem 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.8rem;margin:1rem 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:.8rem;margin:0 0 1.5rem}
.stat .k,.card-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:600}
.stat .v,.card-value{font-size:1.65rem;font-weight:620;font-variant-numeric:tabular-nums;color:var(--text-strong);letter-spacing:-.02em}
.card-value small,.card-sub{font-size:.8rem;color:var(--muted);font-weight:400}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.55rem 0;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:0}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:.4rem 1.5rem;margin:1rem 0}.kv dt{color:var(--muted)}.kv dd{margin:0;overflow-wrap:anywhere}
.body-text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;background:var(--surface);border:1px solid var(--line);
border-radius:var(--r-lg);padding:1.1rem 1.3rem}
.answer{background:linear-gradient(180deg,var(--accent-soft),transparent 70%),var(--surface);padding:1.2rem 1.4rem;border-radius:var(--r-lg);
border:1px solid var(--accent-line);line-height:1.7;margin:1.2rem 0;animation:rise .3s var(--ease)}
.answer ul{margin:.5rem 0}.answer li{margin-bottom:.2rem}
ul.sources{padding-left:1.1rem}ul.sources li{margin-bottom:.7rem}
.error{background:var(--err-bg);padding:.8rem 1rem;border-radius:var(--r-md)}
.warning{border-left:3px solid var(--warn);padding:.6rem .9rem;color:var(--warn);background:var(--warn-bg);border-radius:0 var(--r-sm) var(--r-sm) 0}
table{width:100%;border-collapse:collapse}
table.data{font-size:.88rem}
table.data th{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600;text-align:left;
padding:.5rem .8rem;border-bottom:1px solid var(--line-strong);white-space:nowrap}
table.data td{padding:.65rem .8rem;border-bottom:1px solid var(--line);vertical-align:top;transition:background var(--fast)}
table.data tr:hover td{background:rgba(255,255,255,.025)}
tr.row-link{cursor:pointer}
td.preview{max-width:0;width:100%;min-width:12rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.preview a{color:inherit}
.who-cell{min-width:9rem;max-width:14rem}.who-cell div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.day{padding:1.4rem .8rem .3rem;border-bottom:0}
.filter-bar{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:0 0 1rem}
.filter-bar>*{margin:0}.filter-bar input[type=search]{flex:1 1 16rem;min-width:0;width:auto}
.filter-bar select{flex:0 1 14rem;width:auto;min-width:0}.filter-bar label{display:flex;align-items:center;white-space:nowrap}
.filter-bar button{flex:0 0 auto}
@media (max-width:640px){
table.data thead{display:none}table.data,table.data tbody{display:block}
table.data tr.ev{display:grid;grid-template-columns:1.4rem 1fr auto auto;grid-template-areas:"text text text text" "src who time st";
gap:.25rem .6rem;align-items:center;padding:.85rem .2rem;border-bottom:1px solid var(--line)}
table.data tr.ev td{display:block;border:0;padding:0;min-width:0}
table.data tr.ev td.preview{grid-area:text;white-space:normal;max-width:none;min-width:0;font-size:.95rem;display:-webkit-box;
-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
table.data tr.ev td.c-src{grid-area:src}table.data tr.ev td.who-cell{grid-area:who;max-width:none}
table.data tr.ev td.c-time{grid-area:time;font-size:.8rem}table.data tr.ev td.c-st{grid-area:st}
table.data tr.ev td:nth-child(n+6){display:none}
table.data tr.day-fb{display:block}}
.htmx-indicator{display:none}.htmx-request .htmx-indicator,.htmx-request.htmx-indicator{display:block}
.skeleton{position:relative;overflow:hidden;background:var(--surface-2);border-radius:var(--r-sm);min-height:1rem}
.skeleton::after{content:"";position:absolute;inset:0;transform:translateX(-100%);
background:linear-gradient(90deg,transparent,rgba(255,255,255,.07),transparent);animation:shimmer 1.3s infinite}
@keyframes shimmer{to{transform:translateX(100%)}}
.skel-stack{display:grid;gap:.6rem;margin:1.2rem 0}.skel-stack .skeleton:nth-child(1){height:1.6rem;width:55%}
.skel-stack .skeleton:nth-child(2){height:1rem;width:92%}.skel-stack .skeleton:nth-child(3){height:1rem;width:78%}
.empty{text-align:center;color:var(--muted);padding:2.4rem 1rem;border:1px dashed var(--line-strong);border-radius:var(--r-lg)}
.empty strong{display:block;color:var(--text);font-size:1.02rem;margin-bottom:.3rem}
dialog.dlg{width:min(27rem,calc(100vw - 2rem));padding:1.4rem 1.5rem;color:var(--text);background:var(--glass);
backdrop-filter:blur(22px) saturate(1.5);-webkit-backdrop-filter:blur(22px) saturate(1.5);border:1px solid var(--line-strong);
border-radius:var(--r-lg);box-shadow:0 30px 90px rgba(0,0,0,.65)}
dialog.dlg[open]{animation:pop .2s var(--ease)}
dialog.dlg::backdrop{background:rgba(3,4,7,.62);backdrop-filter:blur(3px)}
dialog.dlg h3{margin:0 0 .5rem}dialog.dlg p{color:var(--muted);margin:0 0 1.2rem;overflow-wrap:anywhere}
dialog.dlg .dlg-actions{display:flex;gap:.6rem;justify-content:flex-end}
@keyframes pop{from{opacity:0;transform:translateY(8px) scale(.97)}to{opacity:1;transform:none}}
#toasts{position:fixed;right:1rem;bottom:1rem;z-index:100;display:grid;gap:.5rem;max-width:min(24rem,calc(100vw - 2rem))}
.toast{display:flex;align-items:center;gap:.8rem;padding:.7rem .8rem .7rem 1rem;background:var(--glass);
backdrop-filter:blur(18px) saturate(1.5);border:1px solid var(--line-strong);border-radius:var(--r-md);box-shadow:var(--shadow-2);
animation:pop .24s var(--ease)}
.toast.err{border-color:rgba(255,122,136,.5)}.toast.ok{border-color:rgba(95,214,155,.4)}
.toast span{flex:1;overflow-wrap:anywhere}.toast button{padding:.25rem .65rem;font-size:.8rem}
"""
