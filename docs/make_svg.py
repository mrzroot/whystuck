#!/usr/bin/env python3
"""Render a captured ANSI terminal transcript to an SVG (no dependencies).

    python docs/make_svg.py docs/demo-excerpt.ansi docs/demo.svg

The input is the *real* output of `whystuck --demo --color always`; nothing is added,
reordered or edited. Only SGR colour/bold/dim codes are interpreted.
"""
import html
import re
import sys

COLORS = {"31": "#ff6b6b", "32": "#69db7c", "33": "#ffd43b", "36": "#66d9e8"}
FG, BG, DIM = "#d8dee9", "#12151c", "#7b8394"
CHAR_W, LINE_H, PAD_X, TOP = 8.8, 19, 20, 52


def parse(line):
    """-> list of (text, color, bold, dim)"""
    out, color, bold, dim = [], FG, False, False
    pos = 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", line):
        if m.start() > pos:
            out.append((line[pos:m.start()], color, bold, dim))
        pos = m.end()
        for code in (m.group(1) or "0").split(";"):
            if code in ("0", ""):
                color, bold, dim = FG, False, False
            elif code == "1":
                bold = True
            elif code == "2":
                dim = True
            elif code in COLORS:
                color = COLORS[code]
    if pos < len(line):
        out.append((line[pos:], color, bold, dim))
    return out


def main(src, dst, title="whystuck --demo"):
    raw = open(src, encoding="utf-8").read().replace("\r\n", "\n").rstrip("\n")
    lines = raw.split("\n")
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]
    cols = max(len(p) for p in plain)
    width = int(cols * CHAR_W + 2 * PAD_X)
    height = TOP + len(lines) * LINE_H + 24
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Terminal recording of {html.escape(title)}">',
         f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
         f'<rect width="{width}" height="34" rx="10" fill="#1d222c"/><rect y="24" width="{width}" height="10" fill="#1d222c"/>',
         '<circle cx="20" cy="17" r="6" fill="#ff5f56"/><circle cx="40" cy="17" r="6" fill="#ffbd2e"/><circle cx="60" cy="17" r="6" fill="#27c93f"/>',
         f'<text x="{width // 2}" y="22" fill="{DIM}" font-family="monospace" font-size="12" text-anchor="middle">{html.escape(title)}</text>']
    for i, ln in enumerate(lines):
        y = TOP + i * LINE_H
        spans = []
        for text, color, bold, dim in parse(ln):
            fill = DIM if dim and color == FG else color
            w = ' font-weight="bold"' if bold else ""
            spans.append(f'<tspan fill="{fill}"{w}>{html.escape(text)}</tspan>')
        if spans:
            o.append(f'<text x="{PAD_X}" y="{y}" font-family="DejaVu Sans Mono, Menlo, Consolas, monospace" font-size="14" '
                     f'xml:space="preserve">{"".join(spans)}</text>')
    o.append("</svg>")
    open(dst, "w", encoding="utf-8").write("\n".join(o) + "\n")
    print(f"wrote {dst}: {len(lines)} lines, {cols} columns")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], *(sys.argv[3:4]))
