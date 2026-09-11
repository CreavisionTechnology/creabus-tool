# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Regenerate the application icons in assets/ from the logo SVG.

    pip install cairosvg pillow
    python tools/make_assets.py

Only the CreaBus Tool mark is generated here, from
``assets/creabus-tool-logo.svg``, which is the source of truth for it. The
outputs are committed so that neither building nor running the app needs these
libraries - this script exists so the binaries in assets/ are reproducible
rather than mysterious.

The two ``creavision-*.png`` files are not generated here. They are the
company mark, supplied by Creavision Technology, and only the small sizes the
window actually draws are kept in this repository.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")
SOURCE = os.path.join(ASSETS, "creabus-tool-logo.svg")

# What the UI, the window manager and the installers ask for.
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
# Windows .ico carries every size a shell might want in one file.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    try:
        import cairosvg  # noqa: PLC0415 - a tool-only dependency
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        print("this script needs cairosvg and pillow:\n"
              "    pip install cairosvg pillow", file=sys.stderr)
        return 1

    if not os.path.exists(SOURCE):
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1

    for size in PNG_SIZES:
        target = os.path.join(ASSETS, f"icon-{size}.png")
        cairosvg.svg2png(url=SOURCE, write_to=target,
                         output_width=size, output_height=size)
        print(f"  wrote assets/icon-{size}.png")

    # The .ico has to be built from the largest render: Pillow can scale a
    # source down into the sizes it writes, but never up.
    base = Image.open(os.path.join(ASSETS, "icon-256.png")).convert("RGBA")
    base.save(os.path.join(ASSETS, "creabus-tool.ico"), format="ICO",
              sizes=[(s, s) for s in ICO_SIZES])
    print("  wrote assets/creabus-tool.ico")

    icns = Image.open(os.path.join(ASSETS, "icon-512.png")).convert("RGBA")
    icns.save(os.path.join(ASSETS, "creabus-tool.icns"), format="ICNS")
    print("  wrote assets/creabus-tool.icns")

    print("\nicons regenerated in assets/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
