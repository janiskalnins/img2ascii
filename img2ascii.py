#!/usr/bin/env python3
"""
img2ascii.py - Convert a PNG/ICO/JPG/etc image to ASCII art for terminal use.

Usage:
    python3 img2ascii.py <image_path> [--width 100] [--color] [--invert]
                          [--charset detailed] [--dither floyd-steinberg]

Options:
    --width N       Output width in characters (default: 80)
    --color         Output ANSI 24-bit color codes (colored ASCII, great for modern terminals)
    --invert        Invert brightness mapping (use for dark-background terminals if it looks wrong)
    --charset NAME  'simple' (@%#*+=-:. ), 'detailed' (wider ramp of characters),
                    or 'blocks' (░▒▓█)
    --dither NAME   'none' (default), 'floyd-steinberg', 'atkinson', or 'ordered'
                    Smooths gradients into a shaded texture instead of flat
                    bands of one character - most noticeable on soft
                    shadows/highlights and smooth backgrounds.
    --bg R,G,B      Background color to composite transparent areas onto (default: white)
    --bias FLOAT    Density bias (gamma) applied to the brightness-to-character mapping
                    (default: 1.0, linear). <1 biases toward denser/darker characters
                    (more ink); >1 biases toward lighter/sparser ones.
    --out FILE      Also save the rendered output (color ANSI codes or plain text) to a file
"""

import sys
import argparse
from PIL import Image

CHARSETS = {
    "simple": " .:-=+*#%@",
    "detailed": ".`^,:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "blocks": " ░▒▓█",
}

# Font cells are roughly 2x taller than wide, so we compress vertically
FONT_ASPECT = 0.55

# 4x4 Bayer matrix, normalized to 0..1, used for ordered dithering
BAYER_4X4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]
BAYER_N = 16  # number of distinct threshold levels in the matrix above


def load_image(path):
    im = Image.open(path)
    im = im.convert("RGBA")
    return im


def flatten_on_background(im, bg=(255, 255, 255)):
    """Composite RGBA onto a solid background so transparency doesn't turn black."""
    background = Image.new("RGBA", im.size, bg + (255,))
    return Image.alpha_composite(background, im).convert("RGB")


def resize_for_ascii(im, width):
    w, h = im.size
    aspect = h / w
    new_h = max(1, int(width * aspect * FONT_ASPECT))
    return im.resize((width, new_h))


def get_gray_grid(im):
    """Return grayscale values as a mutable 2D list of floats (for error diffusion)."""
    gray = im.convert("L")
    w, h = gray.size
    pixels = list(gray.get_flattened_data())
    grid = [[float(pixels[y * w + x]) for x in range(w)] for y in range(h)]
    return grid, w, h


def clamp(v, lo=0.0, hi=255.0):
    return lo if v < lo else hi if v > hi else v


def quantize_level(value, n_levels, bias=1.0):
    """Map a 0..255 gray value to a character-index level 0..n_levels.

    bias applies a gamma curve before quantizing: <1 pushes more values
    toward the denser end of the ramp, >1 toward the sparser end.
    """
    idx = int(round((value / 255) ** bias * n_levels))
    return max(0, min(n_levels, idx))


def level_to_value(level, n_levels, bias=1.0):
    """Inverse of quantize_level, used by error-diffusion ditherers to
    compute the residual error to spread to neighboring pixels."""
    return (level / n_levels) ** (1 / bias) * 255


def dither_none(grid, w, h, n_levels, bias=1.0):
    return [[quantize_level(grid[y][x], n_levels, bias) for x in range(w)] for y in range(h)]


def dither_floyd_steinberg(grid, w, h, n_levels, bias=1.0):
    # Work on a copy so we don't mutate the caller's grid
    g = [row[:] for row in grid]
    levels = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            old = clamp(g[y][x])
            level = quantize_level(old, n_levels, bias)
            new = level_to_value(level, n_levels, bias)
            levels[y][x] = level
            err = old - new
            if x + 1 < w:
                g[y][x + 1] += err * 7 / 16
            if y + 1 < h:
                if x - 1 >= 0:
                    g[y + 1][x - 1] += err * 3 / 16
                g[y + 1][x] += err * 5 / 16
                if x + 1 < w:
                    g[y + 1][x + 1] += err * 1 / 16
    return levels


def dither_atkinson(grid, w, h, n_levels, bias=1.0):
    # Atkinson only diffuses 6/8 of the error (punchier, higher-contrast result)
    g = [row[:] for row in grid]
    levels = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            old = clamp(g[y][x])
            level = quantize_level(old, n_levels, bias)
            new = level_to_value(level, n_levels, bias)
            levels[y][x] = level
            err = (old - new) / 8
            for dx, dy in [(1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    g[ny][nx] += err
    return levels


def dither_ordered(grid, w, h, n_levels, bias=1.0):
    # Bayer/ordered dithering: no error carried between pixels, just a
    # fixed repeating threshold pattern - faster, more mechanical/halftone look
    levels = [[0] * w for _ in range(h)]
    step = 255 / n_levels
    for y in range(h):
        for x in range(w):
            threshold = (BAYER_4X4[y % 4][x % 4] / BAYER_N - 0.5) * step
            val = clamp(grid[y][x] + threshold)
            levels[y][x] = quantize_level(val, n_levels, bias)
    return levels


DITHERERS = {
    "none": dither_none,
    "floyd-steinberg": dither_floyd_steinberg,
    "atkinson": dither_atkinson,
    "ordered": dither_ordered,
}


def levels_to_ascii(levels, w, h, charset, invert=False):
    chars = charset[::-1] if invert else charset
    lines = []
    for y in range(h):
        row = [chars[levels[y][x]] for x in range(w)]
        lines.append("".join(row))
    return "\n".join(lines)


def levels_to_ascii_color(levels, im_rgb, w, h, charset, invert=False):
    chars = charset[::-1] if invert else charset
    rgb_pixels = list(im_rgb.get_flattened_data())
    lines = []
    for y in range(h):
        row = []
        for x in range(w):
            r, g, b = rgb_pixels[y * w + x]
            ch = chars[levels[y][x]]
            row.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
        lines.append("".join(row) + "\x1b[0m")
    return "\n".join(lines)


def main():
    # Also guard printing to the console itself: on Windows, sys.stdout can
    # default to the same locale codepage as file writes, which would mangle
    # block characters printed directly to the terminal (not just --out files).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="Convert an image to ASCII art")
    ap.add_argument("image")
    ap.add_argument("--width", type=int, default=80)
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--charset", default="detailed", choices=list(CHARSETS.keys()))
    ap.add_argument("--dither", default="none", choices=list(DITHERERS.keys()))
    ap.add_argument("--bg", default="255,255,255", help="Background RGB for transparent areas, e.g. 0,0,0")
    ap.add_argument("--bias", type=float, default=1.0,
                     help="Density bias (gamma) for the brightness-to-character mapping: "
                          "<1 biases toward denser characters, >1 toward lighter ones (default: 1.0)")
    ap.add_argument("--out", default=None, help="Save rendered output (color or plain) to this file too")
    args = ap.parse_args()

    if args.bias <= 0:
        ap.error("--bias must be > 0")

    bg = tuple(int(x) for x in args.bg.split(","))
    charset = CHARSETS[args.charset]
    n_levels = len(charset) - 1

    im = load_image(args.image)
    flat = flatten_on_background(im, bg)
    small = resize_for_ascii(flat, args.width)

    gray_grid, w, h = get_gray_grid(small)
    ditherer = DITHERERS[args.dither]
    levels = ditherer(gray_grid, w, h, n_levels, args.bias)

    plain = levels_to_ascii(levels, w, h, charset, invert=args.invert)

    if args.color:
        colored = levels_to_ascii_color(levels, small, w, h, charset, invert=args.invert)
        print(colored)
    else:
        print(plain)

    if args.out:
        # Save whichever version was generated (color ANSI codes or plain text).
        # A .txt file with ANSI codes displays correctly when printed in a
        # terminal that supports 24-bit color, e.g.: cat file.txt
        content = colored if args.color else plain
        # Force UTF-8 explicitly: on Windows, Python's default text encoding
        # is the system locale codepage (often cp1252), not UTF-8. Without
        # this, Unicode block characters (--charset blocks) get silently
        # replaced with '?' at write time, regardless of terminal chcp settings.
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(content + "\n")


if __name__ == "__main__":
    main()
