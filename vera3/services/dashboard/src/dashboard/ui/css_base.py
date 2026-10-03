"""Основа дизайн-системы: токены, сброс, типографика, оболочка и навигация.

Тёмная тема одна. Палитра — нейтрали с лёгким синим оттенком и ОДИН акцент
(индиго); зелёный/жёлтый/красный остаются только статусами. Глубина — тонкие
границы на белом с малой прозрачностью и стекло (`backdrop-filter`) у навигации,
диалогов и боковых панелей. Движение короткое (120–220 мс) и выключается при
`prefers-reduced-motion`. Псевдонимы `--vera-*` оставлены: на них ссылается
страничный CSS и скрипты.
"""
from __future__ import annotations

TOKENS_CSS = """
:root{color-scheme:dark;
--bg:#07080b;--bg-raised:#0c0e13;--bg-inset:#090a0e;--surface:#10121a;--surface-2:#161924;--surface-3:#1d212e;
--line:rgba(255,255,255,.075);--line-strong:rgba(255,255,255,.16);
--text:#e6e9f0;--text-strong:#fff;--muted:#8d95a6;--faint:#5c6475;
--accent:#8b8cff;--accent-strong:#a5a6ff;--accent-ink:#0b0b1f;--accent-soft:rgba(139,140,255,.14);--accent-line:rgba(139,140,255,.42);
--ok:#5fd69b;--warn:#f4c26b;--err:#ff7a88;
--ok-bg:rgba(95,214,155,.13);--warn-bg:rgba(244,194,107,.13);--err-bg:rgba(255,122,136,.13);
--r-sm:8px;--r-md:12px;--r-lg:18px;--r-pill:999px;
--shadow-1:0 1px 0 rgba(255,255,255,.04) inset,0 1px 2px rgba(0,0,0,.4);
--shadow-2:0 1px 0 rgba(255,255,255,.05) inset,0 12px 40px rgba(0,0,0,.45);
--glass:rgba(14,16,22,.72);--ease:cubic-bezier(.22,.8,.24,1);--fast:.14s;--mid:.22s;
--font:ui-sans-serif,"Inter","SF Pro Text","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
--mono:ui-monospace,"JetBrains Mono","SF Mono",Consolas,monospace;
--vera-ok:var(--ok);--vera-warn:var(--warn);--vera-err:var(--err);--vera-muted:var(--muted);
--vera-surface:var(--surface);--vera-line:var(--line);--vera-ok-bg:var(--ok-bg);
--vera-warn-bg:var(--warn-bg);--vera-err-bg:var(--err-bg);--vera-on-err:#2b0a0c}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;
animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
"""

BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-padding-top:5rem}
body{margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);-webkit-font-smoothing:antialiased;
background:radial-gradient(900px 480px at 12% -8%,rgba(139,140,255,.13),transparent 62%),
radial-gradient(700px 420px at 100% 0,rgba(95,214,200,.05),transparent 60%),var(--bg);background-attachment:fixed}
@view-transition{navigation:auto}
main.container{max-width:68rem;margin:0 auto;padding:0 1.25rem 3rem;animation:rise .38s var(--ease) both}
main.container.wide{max-width:96rem}
main.narrow{max-width:30rem;margin:12vh auto 0}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
h1,h2,h3,h4{margin:0 0 .5em;line-height:1.2;color:var(--text-strong);letter-spacing:-.015em;font-weight:620}
h1{font-size:1.9rem}h2{font-size:1.45rem}h3{font-size:1.1rem}
h4{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:1.6rem 0 .6rem}
p{margin:0 0 .9rem}
a{color:var(--accent-strong);text-decoration:none;transition:color var(--fast)}
a:hover{color:var(--text-strong)}
small,.small{font-size:.85rem}
code,kbd,pre{font-family:var(--mono);font-size:.86em}
code{background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:.08em .4em}
pre{background:var(--bg-inset);border:1px solid var(--line);border-radius:var(--r-md);padding:1rem;overflow:auto}
kbd{background:var(--surface-3);border:1px solid var(--line-strong);border-bottom-width:2px;border-radius:5px;padding:.05em .4em}
hr{border:0;border-top:1px solid var(--line);margin:1.4rem 0}
blockquote{margin:.6rem 0;padding:.1rem 0 .1rem .9rem;border-left:2px solid var(--accent-line);color:var(--muted)}
dl{margin:0}
::selection{background:var(--accent-soft);color:var(--text-strong)}
:focus{outline:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--r-sm)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:8px;border:2px solid var(--bg)}
.muted,.mute{color:var(--muted)}.small{font-size:.85rem}.nowrap{white-space:nowrap}.err,.error{color:var(--err)}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.overflow-auto{overflow-x:auto}
.tz-note{margin-top:3rem;font-size:.72rem;text-align:center;color:var(--faint)}
"""

NAV_CSS = """
nav.top{position:sticky;top:.7rem;z-index:30;display:flex;align-items:center;justify-content:space-between;
gap:.6rem;margin:.7rem 0 1.8rem;padding:.4rem .5rem .4rem .9rem;background:var(--glass);
backdrop-filter:blur(16px) saturate(1.5);-webkit-backdrop-filter:blur(16px) saturate(1.5);
border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--shadow-2)}
nav.top ul{display:flex;align-items:center;gap:.15rem;list-style:none;margin:0;padding:0;flex-wrap:wrap}
nav.top li{margin:0;padding:0}
nav.top .brand{display:flex;align-items:center;gap:.55rem;font-weight:650;letter-spacing:-.01em;color:var(--text-strong);margin-right:.7rem}
nav.top .orb{width:1.05rem;height:1.05rem;border-radius:50%;background:conic-gradient(from 210deg,#8b8cff,#5fd6c8,#c98bff,#8b8cff);
box-shadow:0 0 18px rgba(139,140,255,.55)}
nav.top a{display:block;padding:.38rem .8rem;border-radius:var(--r-pill);color:var(--muted);font-size:.9rem;
transition:background var(--fast),color var(--fast)}
nav.top a:hover{color:var(--text-strong);background:rgba(255,255,255,.05)}
nav.top a[aria-current=page]{color:var(--text-strong);background:var(--accent-soft);box-shadow:0 0 0 1px var(--accent-line) inset}
nav.top .out{font-size:.82rem}
nav.top svg{width:1rem;height:1rem;vertical-align:-.15em}
@media (max-width:640px){nav.top{top:.3rem;border-radius:var(--r-md);padding:.35rem}
nav.top .brand span.word{display:none}nav.top a{padding:.4rem .6rem;font-size:.85rem}}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin:0 0 1.2rem}
.page-head h1,.page-head h2{margin:0}.page-head p{margin:.2rem 0 0;color:var(--muted)}
.crumb{font-size:.85rem;margin:0 0 .6rem;color:var(--muted)}
.day{margin:1.6rem 0 .4rem;font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:600}
"""
