"""Стили страницы «Люди». Холст — на всю ширину; панель, поиск, легенда и зум
плавают поверх него стеклом и размера холста не меняют (см. `graph_script_core`)."""
from __future__ import annotations

GRAPH_CSS = """
.g-head p{margin:.2rem 0 0;font-size:.88rem;min-height:1.3rem}
.g-head-actions{display:flex;gap:.5rem;flex-wrap:wrap}
.g-stage{position:relative;height:clamp(30rem,calc(100vh - 11rem),62rem);border:1px solid var(--line);border-radius:var(--r-lg);
overflow:hidden;background:radial-gradient(120% 90% at 50% 0,#12142080,transparent 70%),var(--bg-inset);box-shadow:var(--shadow-2)}
.g-canvas{position:absolute;inset:0;transition:opacity var(--mid)}.g-canvas.loading{opacity:.45}
.g-toolbar{position:absolute;top:.8rem;left:.8rem;right:.8rem;z-index:5;display:flex;gap:.6rem;align-items:flex-start;pointer-events:none}
.g-toolbar>*{pointer-events:auto}
.g-search{position:relative;flex:0 1 22rem;margin:0}
.g-search input{margin:0;background:var(--glass);backdrop-filter:blur(14px);padding-left:2.2rem;
background-image:radial-gradient(circle at 10px 10px,transparent 4px,var(--muted) 4.5px,var(--muted) 6px,transparent 6.5px);
background-repeat:no-repeat;background-position:.8rem 50%;background-size:20px 20px}
#g-suggest{position:absolute;top:calc(100% + .35rem);left:0;right:0;margin:0;padding:.3rem;list-style:none;background:var(--glass);
backdrop-filter:blur(20px) saturate(1.5);border:1px solid var(--line-strong);border-radius:var(--r-md);box-shadow:var(--shadow-2);z-index:8}
#g-suggest li{display:flex;align-items:center;gap:.6rem;padding:.4rem .6rem;border-radius:var(--r-sm);cursor:pointer;margin:0}
#g-suggest li.on,#g-suggest li:hover{background:var(--accent-soft)}
#g-suggest img{width:1.5rem;height:1.5rem;border-radius:50%}#g-suggest span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#g-suggest small{color:var(--muted)}
.g-menu{margin:0 0 0 auto;padding:0;background:var(--glass);backdrop-filter:blur(14px);position:relative}
.g-menu>summary{padding:.55rem .9rem}
.g-menu[open]{padding:0}
.g-menu-body{position:absolute;right:0;top:calc(100% + .4rem);width:17rem;padding:1rem;display:grid;gap:.7rem;background:var(--glass);
backdrop-filter:blur(22px) saturate(1.5);border:1px solid var(--line-strong);border-radius:var(--r-md);box-shadow:var(--shadow-2)}
.g-menu-body label,.g-menu-body select,.g-menu-body form{margin:0}
.g-menu-body select{margin-top:.3rem}
.g-legend-wrap{position:absolute;left:.8rem;bottom:.8rem;z-index:4;display:flex;flex-direction:column-reverse;gap:.4rem;align-items:flex-start;max-width:calc(100% - 5rem)}
.g-legend-wrap>button{background:var(--glass);backdrop-filter:blur(14px)}
.g-legend{display:flex;gap:.4rem;flex-wrap:wrap;padding:.6rem;background:var(--glass);backdrop-filter:blur(20px) saturate(1.5);
border:1px solid var(--line-strong);border-radius:var(--r-md);box-shadow:var(--shadow-2)}
.g-legend[hidden]{display:none}
.g-legend .chip{font-size:.74rem;padding:.05rem .55rem}
.swatch{width:.6rem;height:.6rem;border-radius:50%;display:inline-block}
.ln{display:inline-block;width:1rem;height:0;border-top:2px solid var(--muted)}.ln.dashed{border-top-style:dashed}
.g-zoom{position:absolute;right:.8rem;bottom:.8rem;z-index:4;display:grid;gap:.3rem}
.g-zoom button{width:2.2rem;height:2.2rem;padding:0;font-size:1.1rem;background:var(--glass);backdrop-filter:blur(14px)}
.g-count{position:absolute;right:.9rem;top:3.9rem;z-index:3;font-size:.72rem;color:var(--faint);pointer-events:none}
.g-panel{position:absolute;top:.8rem;right:.8rem;bottom:.8rem;width:23.5rem;z-index:7;overflow:auto;padding:1.15rem;background:var(--glass);
backdrop-filter:blur(24px) saturate(1.6);-webkit-backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-strong);
border-radius:var(--r-lg);box-shadow:0 24px 70px rgba(0,0,0,.6);transform:translateX(0);opacity:1;
transition:transform var(--mid) var(--ease),opacity var(--mid)}
.g-panel[hidden]{display:block;transform:translateX(110%);opacity:0;pointer-events:none}
.g-panel h3{margin:0;font-size:1.2rem;overflow-wrap:anywhere}
.g-panel h4{margin:1.3rem 0 .5rem}
.g-list{list-style:none;padding:0;margin:0}.g-list>li{margin:0 0 .5rem;overflow-wrap:anywhere}
.g-panel-head{display:flex;gap:.8rem;align-items:center}
.g-panel-head img{width:3.4rem;height:3.4rem;border-radius:50%;flex:none;box-shadow:0 0 0 2px var(--accent-line),0 0 24px rgba(139,140,255,.35)}
.g-close{margin-left:auto;align-self:flex-start;padding:.2rem .55rem}
.g-chips{display:flex;gap:.35rem;flex-wrap:wrap;margin:.7rem 0 0}
.g-stats{display:flex;gap:.5rem;margin:.9rem 0 0}
.g-stat{flex:1;padding:.55rem .7rem;background:rgba(255,255,255,.035);border:1px solid var(--line);border-radius:var(--r-md);text-align:center}
.g-stat b{display:block;font-size:1.25rem;color:var(--text-strong);font-variant-numeric:tabular-nums}.g-stat span{font-size:.7rem;color:var(--muted)}
.g-conn{padding:.7rem .8rem;background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:var(--r-md);transition:border-color var(--fast)}
.g-conn:hover{border-color:var(--line-strong)}.g-conn.flash{animation:flash 1.6s var(--ease)}
@keyframes flash{0%,40%{border-color:var(--accent);background:var(--accent-soft)}}
.g-conn-head{display:flex;align-items:center;gap:.55rem;margin-bottom:.4rem}.g-conn-head img{width:1.6rem;height:1.6rem;border-radius:50%}
.g-conn-head a{font-weight:600;flex:1;color:var(--text-strong)}
.g-roles{display:flex;gap:.3rem;flex-wrap:wrap;margin:.2rem 0 .3rem}
.g-weight{width:3.2rem;height:.3rem;border-radius:var(--r-pill);background:var(--surface-3);overflow:hidden}
.g-weight i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--ok))}
.g-role{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin:.2rem 0}
.g-acts{display:flex;gap:.2rem;opacity:.55;transition:opacity var(--fast)}.g-conn:hover .g-acts,.g-acts:focus-within{opacity:1}
.g-act{padding:.15rem .5rem;font-size:.74rem;color:var(--muted)}.g-act[data-act=break]:hover{color:var(--err);background:var(--err-bg)}
.g-tools{display:flex;gap:.4rem;flex-wrap:wrap;margin:.6rem 0 0}
dialog.dlg-wide{width:min(42rem,calc(100vw - 1.5rem));max-height:calc(100dvh - 2rem);overflow:auto}
.dlg-wide h4{margin:1rem 0 .4rem}.dlg-wide label{margin-top:.8rem}.dlg-wide input[type=search]{margin-bottom:.4rem}
.note-line{font-size:.85rem;color:var(--text);background:var(--accent-soft);border:1px solid var(--accent-line);border-radius:var(--r-md);padding:.5rem .7rem}
.pick{list-style:none;margin:0 0 .6rem;padding:.3rem;border:1px solid var(--line-strong);border-radius:var(--r-md);max-height:14rem;overflow:auto}
.pick li{padding:.4rem .5rem;border-radius:var(--r-sm);cursor:pointer}.pick li:hover{background:var(--accent-soft)}
.pcard{display:flex;gap:.6rem;align-items:center;min-width:0}.pcard img{width:2.2rem;height:2.2rem;border-radius:50%;flex:none}
.pcard>div{min-width:0;overflow-wrap:anywhere}
.mcols{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}.mcols>div{padding:.6rem;border:1px solid var(--line);border-radius:var(--r-md)}
.chk{display:flex;gap:.4rem;align-items:center;margin:0;color:var(--text)}
@media (max-width:640px){.mcols{grid-template-columns:1fr}}
.g-foot{display:flex;gap:.5rem;margin-top:1.2rem;flex-wrap:wrap}.g-foot>*{flex:1}
@media (max-width:760px){.g-head p{display:none}.g-head{margin-bottom:.6rem}.g-stage{height:calc(100dvh - 11rem);min-height:24rem}.g-search{flex:1}
.g-zoom{bottom:auto;top:3.9rem}
.g-panel{top:auto;left:.6rem;right:.6rem;bottom:.6rem;width:auto;max-height:64%}
.g-panel[hidden]{transform:translateY(110%)}.g-count{display:none}.g-legend-wrap{bottom:auto;top:3.9rem;flex-direction:column}}
"""
