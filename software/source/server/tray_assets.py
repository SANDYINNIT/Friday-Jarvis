"""Passive asset paths and deterministic fallback for the approved FRIDAY mark.

No tray, window or thread is initialized by importing this module.
"""
from pathlib import Path

ICON_SIZES = (16, 20, 24, 32, 48, 64)
TOOLTIP = "FRIDAY - Personal assistant"
# Installed pystray 0.19.5 Win32 NOTIFYICONDATAW has WCHAR szTip[128].
assert TOOLTIP.isascii() and len(TOOLTIP) <= 127
ASSET_DIR = Path(__file__).resolve().parent / "ui"
ICON_PATH = ASSET_DIR / "friday-tray.ico"
PNG_PATH = ASSET_DIR / "friday-tray-64.png"


def render_mark(size):
    """Pillow equivalent of ui/friday-mark.svg, verified by the asset builder."""
    if type(size) is not int or size not in ICON_SIZES:
        raise ValueError("Unsupported FRIDAY icon size")
    from PIL import Image, ImageDraw

    scale = size * 8 / 64
    image = Image.new("RGBA", (size * 8, size * 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(v * scale for v in (2, 2, 62, 62)),
                           radius=15 * scale, fill="#173a31")
    points = ((18,47),(18,17),(45,17),(45,24),(26,24),
              (26,31),(41,31),(41,38),(26,38),(26,47))
    draw.polygon([(x * scale,y * scale) for x,y in points], fill="#f7f4e9")
    width = max(1, round(3.5 * scale))
    for line in (((47,8),(47,20)), ((41,14),(53,14))):
        xy = [(x * scale,y * scale) for x,y in line]
        draw.line(xy, fill="#cfed87", width=width)
        for x,y in xy:
            radius = width / 2
            draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill="#cfed87")
    result = image.resize((size,size), Image.Resampling.LANCZOS)
    image.close()
    # Remove sub-visible Lanczos ringing outside the transparent rounded mark.
    alpha = result.getchannel("A")
    clean = alpha.point(lambda value: 0 if value <= 2 else value)
    result.putalpha(clean)
    alpha.close()
    clean.close()
    return result


def load_tray_image():
    """Return a detached RGBA image; fixed package paths, no current-directory lookup."""
    from PIL import Image

    for path in (ICON_PATH, PNG_PATH):
        try:
            with Image.open(path) as source:
                if source.format == "ICO":
                    frame = source.ico.getimage((64,64))
                    try:
                        image = frame.convert("RGBA")
                    finally:
                        frame.close()
                elif source.format == "PNG" and source.size == (64,64):
                    image = source.convert("RGBA")
                else:
                    continue
                image.load()
                if image.size != (64,64):
                    image.close()
                    continue
                return image
        except (OSError, ValueError, KeyError, SyntaxError, Image.DecompressionBombError):
            continue
    # A damaged/missing packaged file must not prevent local assistant startup.
    print("[tray] Packaged icon unavailable; using the FRIDAY mark fallback.",flush=True)
    return render_mark(64)
