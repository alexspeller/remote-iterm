"""Render iTerm pane rects (x, y, w, h in 0..1, top-left) as box-drawing ASCII.

Pure renderer over the exact rect shape ``geometry.pane_layout`` produces. Feed
it the per-tab ``aspect`` so the drawing keeps the tab's real proportions. Output
is a 2x-wide canvas: box-drawing junctions (┌┬┐├┼┤└┴┘) sit on even columns and
horizontal fills on odd columns, so a pane spanning C cells is 2*C+1 chars wide.
"""

# Glyph per intersection, keyed by which arms are present.
# bits: 1=up, 2=right, 4=down, 8=left
_JUNCT = {
    0: " ", 1: "╵", 2: "╶", 3: "└", 4: "╷", 5: "│", 6: "┌", 7: "├",
    8: "╴", 9: "┘", 10: "─", 11: "┴", 12: "┐", 13: "┤", 14: "┬", 15: "┼",
}


def render(rects, labels=None, cols=60, aspect=1.7):
    """Draw ``rects`` (``{key: (x, y, w, h)}``) as an ASCII layout.

    ``labels`` maps the same keys to a list of text lines centered in each pane.
    ``cols`` is the canvas width in cells (output is ~2*cols wide). Returns "" for
    an empty layout.
    """
    if not rects:
        return ""
    labels = labels or {}
    # Terminal cells are ~2x taller than wide; scale rows to keep proportions.
    W = max(1, int(cols))
    H = max(4, round(W / max(0.1, aspect) / 2.0))

    # 1. Own each character cell by the pane covering its center.
    owner = [[None] * W for _ in range(H)]
    for key, (x, y, w, h) in rects.items():
        for r in range(max(0, round(y * H)), min(H, round((y + h) * H))):
            for c in range(max(0, round(x * W)), min(W, round((x + w) * W))):
                owner[r][c] = key

    def own(r, c):
        return owner[r][c] if 0 <= r < H and 0 <= c < W else None

    # Horizontal segment on grid-row R over cell-col C separates above/below;
    # vertical segment on grid-col C over cell-row R separates left/right. The
    # canvas edge counts as a boundary (own() returns None off-grid).
    def hseg(R, C):
        return own(R - 1, C) != own(R, C)

    def vseg(R, C):
        return own(R, C - 1) != own(R, C)

    # 2. Build the 2x-wide canvas.
    wide = [[" "] * (2 * W + 1) for _ in range(H + 1)]
    for R in range(H + 1):
        for C in range(W + 1):
            up = R >= 1 and vseg(R - 1, C)
            down = R < H and vseg(R, C)
            left = C >= 1 and hseg(R, C - 1)
            right = C < W and hseg(R, C)
            bits = (up and 1) | (right and 2) | (down and 4) | (left and 8)
            wide[R][2 * C] = _JUNCT[bits]
        for C in range(W):
            if hseg(R, C):
                wide[R][2 * C + 1] = "─"

    # 3. Overlay centered labels inside each pane region.
    for key, (x, y, w, h) in rects.items():
        c0, c1 = round(x * W), round((x + w) * W)
        r0, r1 = round(y * H), round((y + h) * H)
        inner_w = (c1 - c0) * 2 - 3
        text = [ln for ln in labels.get(key, []) if ln]
        start = (r0 + r1) // 2 - len(text) // 2
        for i, ln in enumerate(text):
            rr = start + i
            if not (r0 < rr < r1) or inner_w <= 0:
                continue
            s = ln[:inner_w]
            base = 2 * c0 + 2 + max(0, (inner_w - len(s)) // 2)
            for j, ch in enumerate(s):
                if 0 <= base + j < len(wide[rr]):
                    wide[rr][base + j] = ch

    return "\n".join("".join(row).rstrip() for row in wide)
