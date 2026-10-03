"""Фавикон: единственный источник — отдаётся на /favicon.svg, /favicon.ico и линкуется со всех страниц."""
from __future__ import annotations

# ─── Favicon (SVG, 32x32 viewBox, scales to 16x16 in tab strips) ────────────
# Visual identity: stylised "V" of two strokes meeting at a bright pulse
# node — events flowing in, settling into memory. Distinct from AIbroker's
# hub-and-spokes icon. Single source of truth: this string is served at
# both /favicon.svg and /favicon.ico, and linked from every HTML page.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0f1115"/>'
    '<line x1="8"  y1="9"  x2="16" y2="22" stroke="#4dabf7" stroke-width="3" stroke-linecap="round"/>'
    '<line x1="24" y1="9"  x2="16" y2="22" stroke="#4dabf7" stroke-width="3" stroke-linecap="round"/>'
    '<circle cx="8"  cy="9"  r="2.5" fill="#4dabf7"/>'
    '<circle cx="24" cy="9"  r="2.5" fill="#4dabf7"/>'
    '<circle cx="16" cy="22" r="3.5" fill="#ffffff"/>'
    '</svg>'
)
FAVICON_LINKS = (
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
    '<link rel="alternate icon" href="/favicon.ico">'
    '<link rel="apple-touch-icon" href="/favicon.svg">'
)
