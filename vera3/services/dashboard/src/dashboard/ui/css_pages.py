"""Страничные стили: главная, «Входящее», дубли, источники, прогресс, настройки,
журнал. Общие компоненты — в `css_controls`; здесь только то, что принадлежит
конкретному экрану."""
from __future__ import annotations

HOME_CSS = """
.status-line{display:inline-flex;align-items:center;margin:0 0 1.4rem;padding:.3rem .8rem;font-size:.85rem;color:var(--muted);
border:1px solid var(--line);border-radius:var(--r-pill);background:rgba(255,255,255,.02)}
.status-line:hover{color:var(--text);border-color:var(--line-strong)}
.hero{margin:clamp(1rem,7vh,4rem) 0 2rem}
.hero h1{font-size:clamp(1.9rem,4.4vw,2.9rem);letter-spacing:-.03em;margin:0 0 .4rem;
background:linear-gradient(180deg,#fff,#a9afc0);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:var(--muted);font-size:1.05rem}
form.ask{display:flex;gap:.6rem;flex-wrap:wrap;padding:.5rem;background:var(--surface);border:1px solid var(--line-strong);
border-radius:var(--r-lg);box-shadow:var(--shadow-2);transition:border-color var(--mid),box-shadow var(--mid)}
form.ask:focus-within{border-color:var(--accent);box-shadow:0 0 0 5px var(--accent-soft),var(--shadow-2)}
form.ask input[type=text]{flex:1;min-width:12rem;margin:0;border:0;background:transparent;font-size:1.1rem;padding:.8rem 1rem;box-shadow:none}
form.ask input[type=text]:focus{box-shadow:none}
form.ask button{padding:.75rem 1.4rem}
.ask-hint{margin:.6rem .4rem 0;font-size:.8rem;color:var(--faint)}
"""

DUPES_CSS = """
.pair{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:.6rem 0}
.cand-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr));gap:1rem;margin:.6rem 0}
.pair-card{margin:0 0 1rem}.pair-card h4{margin:0 0 .2rem;font-size:1rem;text-transform:none;letter-spacing:0;color:var(--text-strong)}
.pair-card p{margin:0 0 .4rem}
.person{border:1px solid var(--line);border-radius:var(--r-md);padding:.8rem;min-width:0;background:rgba(255,255,255,.015)}
.person-head{display:flex;gap:.6rem;align-items:flex-start;overflow-wrap:anywhere}.person-head img{border-radius:50%;flex:none}
.person .badge{margin-left:auto;font-size:.7rem;color:var(--muted);white-space:nowrap}
.person blockquote{font-size:.8rem}
.chips{margin:.4rem 0}
.actions,.bulk,.actions-row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.actions form,.bulk form,.actions-row{margin:0;display:inline-flex;gap:.6rem}
.select-merge{display:flex;gap:.8rem;flex-wrap:wrap;align-items:end}
.select-merge select,.select-merge label{margin:0}
.bulk{margin:.6rem 0 1.2rem}
@media (max-width:640px){.pair{grid-template-columns:1fr}}
"""

SOURCES_CSS = """
.src-list{width:100%;border-collapse:collapse;font-size:.92rem}
.src-list th{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600;text-align:left;padding:0 .8rem .6rem 0}
.src-list td{padding:.9rem .8rem .9rem 0;border-top:1px solid var(--line);vertical-align:middle}
.src-list tr:hover td{background:rgba(255,255,255,.02)}
.src-name{display:flex;align-items:center;gap:.7rem}.src-name .ico{font-size:1.1rem;width:1.5rem;text-align:center}
.src-name a{font-weight:600;color:var(--text-strong)}
.src-how{color:var(--muted);font-size:.78rem;margin-top:.15rem}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.act{text-align:right;white-space:nowrap}
.act a{display:inline-block;font-size:.78rem;padding:.3rem .7rem;border:1px solid var(--line-strong);border-radius:var(--r-sm);color:var(--muted);
transition:all var(--fast)}
.act a:hover{border-color:var(--accent-line);color:var(--accent-strong);background:var(--accent-soft)}
.idle td{opacity:.5}
.head{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;margin:0 0 .3rem}.head h1{margin:0;font-size:1.6rem}
.strip{display:flex;gap:2rem;flex-wrap:wrap;margin:1.2rem 0 .3rem;padding:1.1rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.strip div{min-width:7rem}.strip .k{font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:600}
.strip .v{font-size:1.4rem;font-weight:620;margin-top:.2rem;font-variant-numeric:tabular-nums;color:var(--text-strong)}
.strip .v.v-small{font-size:1rem}
.blocks{display:grid;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));gap:1rem;margin-top:1.4rem}
.blk.wide{grid-column:1/-1}
.blk h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 .8rem}
.blk .hint{color:var(--muted);font-size:.78rem;margin-top:.8rem;line-height:1.5}
.push-right{margin-left:auto}.note{color:var(--muted);font-size:.85rem;margin:.4rem 0 0}
"""

PROGRESS_CSS = """
.prog-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(12.5rem,1fr));gap:.8rem;margin:.9rem 0}
.prog-cell{background:var(--bg-inset);border:1px solid var(--line);border-radius:var(--r-md);padding:.9rem}
.prog-label{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.prog-big{font-size:1.65rem;font-weight:620;margin:.35rem 0 .2rem;color:var(--text-strong);font-variant-numeric:tabular-nums}
.prog-unit{font-size:.8rem;color:var(--muted);font-weight:400;margin-left:.3rem}
.bar{background:var(--bg-inset);height:.5rem;border-radius:var(--r-pill);overflow:hidden;border:1px solid var(--line)}
.bar-fill{background:linear-gradient(90deg,var(--accent),var(--ok));height:100%;transition:width 1s var(--ease)}
.bf-control{display:flex;align-items:center;gap:.8rem;margin:.4rem 0 .9rem;flex-wrap:wrap}
.bf-rate{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:0}
.bf-rate label{font-size:.8rem;color:var(--muted);margin:0}.bf-rate input{width:8.5rem;margin:0;padding:.4rem .7rem}
.bf-btn{padding:.4rem 1rem;font-size:.82rem}.bf-hint{font-size:.8rem;color:var(--muted)}
"""

SETTINGS_CSS = """
.set-row{display:flex;justify-content:space-between;align-items:flex-start;gap:1.2rem;padding:.95rem 0;border-bottom:1px solid var(--line)}
.set-main label{font-weight:600;color:var(--text);font-size:.95rem}
.set-desc{color:var(--muted);font-size:.85rem;margin-top:.2rem;max-width:32rem}
.set-field{white-space:nowrap;display:flex;align-items:center;gap:.4rem}.set-field input,.set-field select{width:8rem;margin:0}
.set-unit{color:var(--muted);font-size:.85rem}
.journal-row{display:flex;gap:1rem;align-items:center;justify-content:space-between;padding:.85rem 0;border-bottom:1px solid var(--line)}
.journal-row:last-child{border-bottom:0}.journal-row.undone .j-what{text-decoration:line-through;color:var(--muted)}
.j-what{font-weight:550;overflow-wrap:anywhere}.j-meta{font-size:.8rem;color:var(--muted);margin-top:.15rem}
"""

PAGES_CSS = HOME_CSS + DUPES_CSS + SOURCES_CSS + PROGRESS_CSS + SETTINGS_CSS
