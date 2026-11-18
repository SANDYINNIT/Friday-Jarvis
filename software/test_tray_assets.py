"""Decode every actual shipped frame and exercise source/package-relative fallback."""
import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest
from source.server import tray_assets as assets


@pytest.mark.parametrize('size',assets.ICON_SIZES)
def test_png_dimensions_transparency_and_mark_contrast(size):
    path=assets.ASSET_DIR/f'friday-tray-{size}.png'
    with Image.open(path) as image:
        image.load();assert image.size==(size,size) and image.mode=='RGBA'
        assert image.getpixel((0,0))[3]==0
        pixels=list(image.getdata())
        assert min(p[3]for p in pixels)==0 and max(p[3]for p in pixels)==255
        assert sum(r>210 and g>210 and b>200 and a>200 for r,g,b,a in pixels)>size*size*.03
        assert any(g>r+10 and g>b+50 and a>100 for r,g,b,a in pixels)


def test_multi_resolution_ico_has_exact_generated_frames():
    with Image.open(assets.ICON_PATH) as icon:
        assert icon.ico.sizes()=={(size,size)for size in assets.ICON_SIZES}
        for size in assets.ICON_SIZES:
            with icon.ico.getimage((size,size)) as frame,Image.open(assets.ASSET_DIR/f'friday-tray-{size}.png') as png:
                assert frame.convert('RGBA').tobytes()==png.tobytes()


def test_manifest_and_approved_source_match():
    root=Path(__file__).parent
    manifest=json.loads((root/'tools/tray_assets/manifest.json').read_text())
    source=root/manifest['source'];reference=root/manifest['reference']
    assert source.read_bytes()==reference.read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest['source_sha256']
    for name,digest in manifest['assets'].items():assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest
    assert 'source/server/ui/*' in (root/'pyproject.toml').read_text(encoding='utf-8-sig')


def test_loader_is_independent_of_working_directory(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    with assets.load_tray_image() as image,Image.open(assets.PNG_PATH) as png:
        assert image.mode=='RGBA' and image.size==(64,64) and image.tobytes()==png.tobytes()


def test_corrupt_ico_uses_valid_png_then_procedural_fallback(tmp_path,monkeypatch):
    bad=tmp_path/'bad.ico';bad.write_bytes(b'corrupt icon')
    monkeypatch.setattr(assets,'ICON_PATH',bad)
    with assets.load_tray_image() as image,assets.render_mark(64) as expected:assert image.tobytes()==expected.tobytes()
    monkeypatch.setattr(assets,'PNG_PATH',tmp_path/'missing.png')
    with assets.load_tray_image() as image,assets.render_mark(64) as expected:assert image.tobytes()==expected.tobytes()


def test_invalid_dimension_asset_cannot_replace_brand(tmp_path,monkeypatch):
    bad=tmp_path/'wrong.png'
    with Image.new('RGBA',(1,1),'red') as image:image.save(bad)
    monkeypatch.setattr(assets,'ICON_PATH',bad);monkeypatch.setattr(assets,'PNG_PATH',bad)
    with assets.load_tray_image() as image:assert image.size==(64,64)


def test_tooltip_has_no_private_or_dynamic_fields():
    assert assets.TOOLTIP=='FRIDAY - Personal assistant'
    assert assets.TOOLTIP.isascii() and len(assets.TOOLTIP.encode('utf-16-le'))//2<=127


def test_unsupported_format_cannot_masquerade_as_packaged_icon(tmp_path,monkeypatch):
    bad=tmp_path/'not-an-icon.ico'
    with Image.new('RGB',(64,64),'red') as image:image.save(bad,format='BMP')
    monkeypatch.setattr(assets,'ICON_PATH',bad);monkeypatch.setattr(assets,'PNG_PATH',bad)
    with assets.load_tray_image() as image,assets.render_mark(64) as expected:assert image.tobytes()==expected.tobytes()
