"""Build the Tracedown logo files: the mark, and the mark + wordmark for light and dark
backgrounds. The wordmark is Inter Display SemiBold converted to outlines, so the SVGs
look the same everywhere (GitHub shows SVGs as images, without web fonts)."""
import os
import sys
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
ORANGE = "#F26419"
FONT = "/usr/share/fonts/opentype/inter/InterDisplay-SemiBold.otf"

# The mark: a trace drops down two steps to a target ring, the cause. Drawn on a 64-unit grid.
def mark(x=0, y=0, s=1.0):
    t = f'transform="translate({x} {y}) scale({s})"'
    return (f'<g {t}><rect x="4" y="4" width="56" height="56" rx="15" fill="{ORANGE}"/>'
            '<line x1="32" y1="15" x2="32" y2="34.5" stroke="#fff" stroke-width="4.5" stroke-linecap="round"/>'
            '<circle cx="32" cy="15" r="4.2" fill="#fff"/><circle cx="32" cy="25.5" r="4.2" fill="#fff"/>'
            '<circle cx="32" cy="44" r="9" fill="none" stroke="#fff" stroke-width="4.5"/>'
            '<circle cx="32" cy="44" r="3.1" fill="#fff"/></g>')

def wordmark(text, size, x, baseline, tracking=-0.012):
    font = TTFont(FONT)
    gs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    upm = font["head"].unitsPerEm
    k = size / upm
    pen = SVGPathPen(gs)
    cx = 0.0
    bounds = BoundsPen(gs)
    for ch in text:
        g = cmap[ord(ch)]
        # y is flipped: font units go up, SVG goes down
        gs[g].draw(TransformPen(pen, (k, 0, 0, -k, x + cx, baseline)))
        gs[g].draw(TransformPen(bounds, (k, 0, 0, -k, x + cx, baseline)))
        cx += hmtx[g][0] * k + tracking * size
    return pen.getCommands(), cx, bounds.bounds

def lockup(text_color, name):
    h = 64
    d, width, b = wordmark("Tracedown", 40, 78, 46)
    w = int(78 + width + 4)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="Tracedown">'
           f'<title>Tracedown</title>{mark()}<path d="{d}" fill="{text_color}"/></svg>')
    open(f"{OUT}/{name}", "w").write(svg)
    return w

open(f"{OUT}/logo-mark.svg", "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" role="img" aria-label="Tracedown">'
    f'<title>Tracedown</title>{mark()}</svg>')
print("light", lockup("#1F2328", "logo-light.svg"), "dark", lockup("#F0F3F6", "logo-dark.svg"))
