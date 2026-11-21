#!/usr/bin/python3
"""Generate the AdGuard VPN GUI icon set (PNG + SVG) with Cairo.

Run from the project root:  python3 tools/make_icons.py
"""
from __future__ import annotations

import math
import os

import cairo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "agvpn", "assets")


def rounded_rect(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def shield_path(ctx, s=1.0, ox=0.0, oy=0.0):
    """Shield outline in a 512x512 coordinate space."""
    def p(x, y):
        return (ox + x * s, oy + y * s)

    ctx.move_to(*p(256, 66))
    ctx.curve_to(*p(318, 96), *p(362, 116), *p(394, 128))
    ctx.curve_to(*p(398, 190), *p(398, 250), *p(386, 300))
    ctx.curve_to(*p(364, 366), *p(316, 414), *p(256, 444))
    ctx.curve_to(*p(196, 414), *p(148, 366), *p(126, 300))
    ctx.curve_to(*p(114, 250), *p(114, 190), *p(118, 128))
    ctx.curve_to(*p(150, 116), *p(194, 96), *p(256, 66))
    ctx.close_path()


def power_glyph(ctx, s=1.0, ox=0.0, oy=0.0, width=25.0, color=(0.04, 0.09, 0.07, 1)):
    cx, cy, r = ox + 256 * s, oy + 262 * s, 80 * s
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_width(width * s)
    ctx.set_source_rgba(*color)
    ctx.arc(cx, cy, r, math.radians(-58), math.radians(238))
    ctx.stroke()
    ctx.move_to(ox + 256 * s, oy + 182 * s)
    ctx.line_to(ox + 256 * s, oy + 252 * s)
    ctx.stroke()


def draw_shield(ctx, s=1.0, ox=0.0, oy=0.0, color_top="#4FE7AE", color_bottom="#16A375",
                glyph=True, stroke=True):
    shield_path(ctx, s, ox, oy)
    grad = cairo.LinearGradient(0, oy + 66 * s, 0, oy + 444 * s)
    grad.add_color_stop_rgb(0, *hex_rgb(color_top))
    grad.add_color_stop_rgb(1, *hex_rgb(color_bottom))
    ctx.set_source(grad)
    ctx.fill_preserve()

    if stroke:
        ctx.set_line_width(3 * s)
        ctx.set_source_rgba(1, 1, 1, 0.22)
        ctx.stroke_preserve()
    ctx.new_path()

    # Soft inner highlight on the upper-left shoulder.
    ctx.save()
    shield_path(ctx, s, ox, oy)
    ctx.clip()
    hl = cairo.LinearGradient(ox + 120 * s, oy + 90 * s, ox + 260 * s, oy + 300 * s)
    hl.add_color_stop_rgba(0, 1, 1, 1, 0.16)
    hl.add_color_stop_rgba(1, 1, 1, 1, 0)
    ctx.set_source(hl)
    ctx.paint()
    ctx.restore()

    if glyph:
        power_glyph(ctx, s, ox, oy, 25, (0.035, 0.085, 0.066, 0.96))


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def render(size, path, background=True, shield_scale=1.0, colors=("#4FE7AE", "#16A375"),
           glyph=True, glow=True):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surf)
    ctx.scale(size / 512.0, size / 512.0)

    if background:
        rounded_rect(ctx, 8, 8, 496, 496, 108)
        bg = cairo.LinearGradient(0, 0, 512, 512)
        bg.add_color_stop_rgb(0, 0.047, 0.086, 0.075)
        bg.add_color_stop_rgb(1, 0.027, 0.051, 0.045)
        ctx.set_source(bg)
        ctx.fill()

        if glow:
            ctx.save()
            rounded_rect(ctx, 8, 8, 496, 496, 108)
            ctx.clip()
            rg = cairo.RadialGradient(256, 210, 30, 256, 240, 290)
            rg.add_color_stop_rgba(0, *hex_rgb(colors[0]), 0.20)
            rg.add_color_stop_rgba(0.55, *hex_rgb(colors[1]), 0.07)
            rg.add_color_stop_rgba(1, 0, 0, 0, 0)
            ctx.set_source(rg)
            ctx.paint()
            ctx.restore()

        # Thin rim light.
        rounded_rect(ctx, 9.5, 9.5, 493, 493, 107)
        ctx.set_line_width(2.4)
        ctx.set_source_rgba(1, 1, 1, 0.07)
        ctx.stroke()
        draw_shield(ctx, 0.80 * shield_scale, 45, 62, *colors, glyph=glyph)
    else:
        draw_shield(ctx, shield_scale, 0, 0, *colors, glyph=glyph)

    ctx.identity_matrix()
    surf.write_to_png(path)


def render_svg(path):
    # A hand-written, crisp scalable version of the same icon.
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0c1613"/><stop offset="1" stop-color="#07100d"/>
    </linearGradient>
    <linearGradient id="shield" x1="0" y1="0.12" x2="0" y2="0.9">
      <stop offset="0" stop-color="#4fe7ae"/><stop offset="1" stop-color="#16a375"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.42" r="0.56">
      <stop offset="0" stop-color="#4fe7ae" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#16a375" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="round"><rect x="8" y="8" width="496" height="496" rx="108"/></clipPath>
  </defs>
  <g clip-path="url(#round)">
    <rect x="8" y="8" width="496" height="496" fill="url(#bg)"/>
    <rect x="8" y="8" width="496" height="496" fill="url(#glow)"/>
  </g>
  <rect x="9.5" y="9.5" width="493" height="493" rx="107" fill="none" stroke="#ffffff" stroke-opacity="0.07" stroke-width="2.4"/>
  <path d="M294 136c34 16 58 28 81 36 4 47 4 92-5 131-18 51-54 88-114 119-60-31-96-68-114-119-9-39-9-84-5-131 23-8 47-20 81-36 19-9 38-18 57-27 19 9 38 18 57 27z"
        transform="translate(0 6) scale(0.82) translate(38 38)"
        fill="url(#shield)" stroke="#ffffff" stroke-opacity="0.22" stroke-width="3.6"/>
  <g fill="none" stroke="#08150f" stroke-width="20" stroke-linecap="round">
    <path d="M211 246a52 52 0 1 0 90 0"/>
    <path d="M256 194v52"/>
  </g>
</svg>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)


def main():
    os.makedirs(ASSETS, exist_ok=True)

    render(512, os.path.join(ASSETS, "icon.png"))
    render(256, os.path.join(ASSETS, "icon-256.png"))
    render(128, os.path.join(ASSETS, "icon-128.png"))
    render(64, os.path.join(ASSETS, "icon-64.png"))
    render(48, os.path.join(ASSETS, "icon-48.png"))
    render(32, os.path.join(ASSETS, "icon-32.png"))
    render_svg(os.path.join(ASSETS, "icon.svg"))

    # Tray icons: no background, slightly compressed shield, high contrast.
    render(64, os.path.join(ASSETS, "tray-connected.png"), background=False,
           shield_scale=0.98, colors=("#57EDB6", "#18AA7C"))
    render(64, os.path.join(ASSETS, "tray-disconnected.png"), background=False,
           shield_scale=0.98, colors=("#A6BBB4", "#6F857E"))
    render(64, os.path.join(ASSETS, "tray-connecting.png"), background=False,
           shield_scale=0.98, colors=("#FFD479", "#E0A02B"))


if __name__ == "__main__":
    main()
