#!/usr/bin/env python3
import math
import pathlib
import random

W, H = 1200, 630
CANVAS, SURFACE, RAISED, LINE = "#08090b", "#0e1014", "#151821", "#282c35"
TEXT, MUTED, FAINT = "#f5f3ee", "#a2a8b3", "#747c89"
RED, GREEN, ACCENT = "#ed6a67", "#57c785", "#b9a0ff"
FONT = "'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace, 'SFMono-Regular', Menlo, monospace"

random.seed(7)
px, py, pw, ph = 64, 214, 1072, 352
cx0, cx1, cy0, cy1 = px + 28, px + pw - 28, py + ph - 44, py + 96
steps = 120
value = 0.0
points = []
for index in range(steps + 1):
    t = index / steps
    burst = 6.0 if 0.33 < t < 0.40 or 0.71 < t < 0.80 else 0.0
    value += 0.35 + 1.6 * t + burst + random.random() * 0.5
    points.append(value)
top = points[-1]
coords = [(cx0 + (cx1 - cx0) * i / steps, cy0 - (cy0 - cy1) * v / top) for i, v in enumerate(points)]
line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
area = f"{line} L{cx1:.1f},{cy0:.1f} L{cx0:.1f},{cy0:.1f} Z"
target_y = cy0 - 6
grid = "".join(
    f'<line x1="{cx0}" x2="{cx1}" y1="{cy0 - (cy0 - cy1) * k / 4:.1f}" y2="{cy0 - (cy0 - cy1) * k / 4:.1f}" stroke="{LINE}" stroke-width="1"/>'
    for k in range(1, 5)
)
ex, ey = coords[-1]
sx, sy, sw, sh = 128, 330, 404, 104

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="rpc-xray: a red line of failed user requests climbs while a flat green line shows where it should be">
  <defs>
    <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{RED}" stop-opacity="0.32"/>
      <stop offset="1" stop-color="{RED}" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="{CANVAS}"/>
  <text x="64" y="72" font-family="{MONO}" font-size="22" fill="{ACCENT}">rpc-xray</text>
  <text x="64" y="132" font-family="{FONT}" font-size="50" font-weight="600" fill="{TEXT}">See every request your RPC provider failed.</text>
  <text x="64" y="176" font-family="{FONT}" font-size="23" fill="{MUTED}">Three lines of code. Real traffic. The errors, stale blocks and slow calls your users hit.</text>
  <rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="6" fill="{SURFACE}" stroke="{LINE}"/>
  <text x="{px + 28}" y="{py + 40}" font-family="{FONT}" font-size="18" fill="{MUTED}">Failed user requests since install</text>
  <text x="{px + 28}" y="{py + 84}" font-family="{FONT}" font-size="44" font-weight="600" fill="{RED}">48,211</text>
  {grid}
  <path d="{area}" fill="url(#fill)"/>
  <path d="{line}" fill="none" stroke="{RED}" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{ex:.1f}" cy="{ey:.1f}" r="6" fill="{RED}"/>
  <line x1="{cx0}" x2="{cx1}" y1="{target_y}" y2="{target_y}" stroke="{GREEN}" stroke-width="3" stroke-dasharray="10 8"/>
  <text x="{cx0 + 8}" y="{target_y - 12}" font-family="{FONT}" font-size="18" font-weight="600" fill="{GREEN}">Where you should be</text>
  <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="6" fill="{RAISED}" stroke="{LINE}"/>
  <rect x="{sx}" y="{sy}" width="5" height="{sh}" rx="2" fill="{RED}"/>
  <text x="{sx + 22}" y="{sy + 30}" font-family="{FONT}" font-size="14" fill="{FAINT}">#incidents  ·  rpc-xray</text>
  <text x="{sx + 22}" y="{sy + 58}" font-family="{FONT}" font-size="18" font-weight="600" fill="{TEXT}">RPC incident: ethereum</text>
  <text x="{sx + 22}" y="{sy + 84}" font-family="{FONT}" font-size="16" fill="{MUTED}">No new block from your provider for 3m 12s</text>
</svg>
'''
pathlib.Path(__file__).with_name("hero.svg").write_text(svg)
print("wrote hero.svg")
