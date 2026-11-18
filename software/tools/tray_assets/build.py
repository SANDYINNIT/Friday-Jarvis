"""Build native-size FRIDAY tray assets with installed Pillow only; no tray/backend import."""
from pathlib import Path
import hashlib
import json
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from source.server.tray_assets import ICON_SIZES, render_mark


def build():
    source = ROOT / "source/server/ui/friday-mark.svg"
    prototype = ROOT / "prototypes/friday-field-notes/tray.svg"
    assert source.read_bytes() == prototype.read_bytes(), "Approved reference and shipped mark differ; review before rendering"
    svg = ET.fromstring(source.read_bytes())
    elements = list(svg)
    assert svg.attrib["viewBox"] == "0 0 64 64" and len(elements)==3
    assert elements[0].attrib == dict(x="2",y="2",width="60",height="60",rx="15",fill="#173a31")
    assert elements[1].attrib == dict(d="M18 47V17h27v7H26v7h15v7H26v9z",fill="#f7f4e9")
    assert elements[2].attrib == {"d":"M47 8v12m-6-6h12","stroke":"#cfed87","stroke-width":"3.5","stroke-linecap":"round"}
    from PIL import Image, ImageDraw
    images = [render_mark(size) for size in ICON_SIZES]
    files=[]
    for size,image in zip(ICON_SIZES,images):
        path=source.parent/f"friday-tray-{size}.png"
        image.save(path);files.append(path)
    ico=source.parent/"friday-tray.ico"
    images[-1].save(ico,format="ICO",sizes=[(s,s) for s in ICON_SIZES],append_images=images[:-1])
    files.append(ico)
    sheet=Image.new("RGB",(700,274),"#f6f5ee")
    draw=ImageDraw.Draw(sheet)
    draw.text((20,13),"FRIDAY - native-size raster assets (not Windows acceptance)",fill="#173a31")
    for y,bg,label in ((58,"#f1f1f1","Light taskbar"),(166,"#202020","Dark taskbar")):
        draw.rectangle((16,y,684,y+86),fill=bg)
        for index,(size,image) in enumerate(zip(ICON_SIZES,images)):
            x=60+index*104
            draw.text((x-8,y-18),f"{size} px",fill="#173a31")
            sheet.paste(image,(x,y+((86-size)//2)),image)
        draw.text((20,y+4),label,fill="#57675e" if y==58 else "#e5e5e5")
    sheet_path=Path(__file__).parent/"contact-sheet.png"
    sheet.save(sheet_path)
    manifest={"source":"source/server/ui/friday-mark.svg","reference":"prototypes/friday-field-notes/tray.svg",
              "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"sizes":ICON_SIZES,
              "method":"Exact approved SVG primitives, Pillow 8x supersampling + Lanczos; alpha <= 2/255 fringe cleared. No font glyphs or external renderers.",
              "assets":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()for p in files}}
    (Path(__file__).parent/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    for image in images:image.close()
    sheet.close()
    print("Built",len(files),"assets and a native-size light/dark contact sheet.")


if __name__=="__main__":build()
