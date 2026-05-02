"""Regenerate src/ai_arena_recap/web/static/favicon.svg from the --accent
color in styles.css. Run this whenever the accent color changes:

    uv run python scripts/regen_favicon.py

The favicon SVG stays checked in (browsers fetch it as a static asset);
this script just keeps its color in sync with the CSS source of truth.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "src" / "ai_arena_recap" / "web" / "static" / "styles.css"
SVG_PATH = ROOT / "src" / "ai_arena_recap" / "web" / "static" / "favicon.svg"

# Bar color stays a favicon-local constant: pure black gives the best
# contrast at 16x16 even though the CSS background is the slightly-lighter
# --bg (#0e1116). Adjust here if you want to bring it back in line.
BAR_COLOR = "#000"


def main() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    m = re.search(r"--accent:\s*(#[0-9a-fA-F]{3,8})\s*;", css)
    if not m:
        raise SystemExit(f"Could not find --accent: in {CSS_PATH}")
    accent = m.group(1)

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">\n'
        f'  <rect width="32" height="32" rx="6" fill="{accent}"/>\n'
        f'  <rect x="7" y="18" width="4" height="8" fill="{BAR_COLOR}"/>\n'
        f'  <rect x="14" y="12" width="4" height="14" fill="{BAR_COLOR}"/>\n'
        f'  <rect x="21" y="6" width="4" height="20" fill="{BAR_COLOR}"/>\n'
        "</svg>\n"
    )
    SVG_PATH.write_text(svg, encoding="utf-8")
    print(f"Wrote {SVG_PATH.relative_to(ROOT)} with accent={accent}, bars={BAR_COLOR}")


if __name__ == "__main__":
    main()
