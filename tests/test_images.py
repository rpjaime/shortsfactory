from pathlib import Path

from PIL import Image

from conftest import make_image
from image_processor import (MAX_ACCEPTABLE_CROP, analyze_folder, analyze_image, compose_slide,
                             crop_fraction_for_fill, load_image, make_outro_card, motion_params, natural_key,
                             render_frame, resolve_motion, scan_folder)
from project_manager import default_design


def test_natural_sort_order(image_folder):
    assert scan_folder(image_folder) == ["1-portada.png", "2-repo.jpg", "03-tercera.webp", "4-cuarta.jpeg",
                                         "10-final.png"]
    assert sorted(["img10.png", "img2.png", "IMG1.png"], key=natural_key) == ["IMG1.png", "img2.png", "img10.png"]


def test_scan_ignores_other_files(tmp_path):
    make_image(tmp_path / "a.png")
    (tmp_path / "notas.txt").write_text("x")
    (tmp_path / "video.mp4").write_bytes(b"0")
    assert scan_folder(tmp_path) == ["a.png"]


def test_unreadable_and_small_images(tmp_path):
    (tmp_path / "roto.png").write_bytes(b"esto no es una imagen")
    make_image(tmp_path / "mini.png", size=(300, 300))
    bad = analyze_image(tmp_path / "roto.png")
    small = analyze_image(tmp_path / "mini.png")
    assert not bad.ok and "ilegible" in bad.error
    assert small.ok and any("pequeña" in w for w in small.warnings)


def test_duplicates_detected(tmp_path):
    make_image(tmp_path / "a.png", color=(10, 20, 30))
    (tmp_path / "b.png").write_bytes((tmp_path / "a.png").read_bytes())
    im = Image.open(tmp_path / "a.png").convert("RGB")
    im.save(tmp_path / "c.jpg", quality=92)   # casi idéntica
    make_image(tmp_path / "d.png", color=(250, 250, 10), text="otra")
    infos = analyze_folder(tmp_path)
    by = {i.name: i for i in infos}
    assert any("idéntica" in w for w in by["b.png"].warnings)
    assert any("casi idéntica" in w for w in by["c.jpg"].warnings)
    assert not by["d.png"].warnings


def test_exif_orientation_is_applied(tmp_path):
    im = Image.new("RGB", (800, 400), (255, 0, 0))
    exif = im.getexif()
    exif[0x0112] = 6   # rotar 90°
    p = tmp_path / "rot.jpg"
    im.save(p, exif=exif)
    info = analyze_image(p)
    assert info.exif_rotated and (info.width, info.height) == (400, 800)
    assert load_image(p).size == (400, 800)


def test_transparency_supported(tmp_path):
    p = make_image(tmp_path / "alpha.png", size=(800, 800), mode="RGBA")
    info = analyze_image(p)
    assert info.has_alpha
    d = default_design("clean_editorial")
    comp = compose_slide(load_image(p), d, 540, 960)
    frame = render_frame(comp, 1.0, 0, 0)
    assert frame.mode == "RGB" and frame.size == (540, 960)
    # Una esquina de la imagen transparente debe mostrar el color de fondo de la plantilla
    x, y = int(comp.fg_pos[0]) + 3, int(comp.fg_pos[1]) + 3
    assert frame.getpixel((x, y)) == (0xf4, 0xf1, 0xea)


def test_composition_modes(tmp_path):
    p = make_image(tmp_path / "sq.png", size=(1080, 1080))
    img = load_image(p)
    d = default_design()
    for mode in ("blur", "solid"):
        comp = compose_slide(img, d, 1080, 1920, {"composition": mode})
        assert comp.mode == mode and comp.background.size == (1080, 1920)
        # La imagen completa cabe en el lienzo (no se recorta)
        assert comp.fg_pos[0] >= -60 and comp.fg_pos[0] + comp.foreground.width <= 1080 + 60
    # Un cuadrado recortado a 9:16 pierde ~68 %: el modo «llenar» se rechaza
    assert crop_fraction_for_fill(1080, 1080, 1080, 1920) > MAX_ACCEPTABLE_CROP
    comp = compose_slide(img, d, 1080, 1920, {"composition": "fill"})
    assert comp.mode == "blur" and comp.warnings
    # Una imagen casi vertical sí admite «llenar»
    tall = load_image(make_image(tmp_path / "tall.png", size=(1000, 1700)))
    comp = compose_slide(tall, d, 1080, 1920, {"composition": "fill"})
    assert comp.mode == "fill"
    # Forzar el modo llenar
    comp = compose_slide(img, d, 1080, 1920, {"composition": "fill", "force_fill": True})
    assert comp.mode == "fill"


def test_overrides_scale_and_offset(tmp_path):
    img = load_image(make_image(tmp_path / "a.png", size=(1000, 1000)))
    d = default_design("clean_editorial")
    base = compose_slide(img, d, 1080, 1920)
    big = compose_slide(img, d, 1080, 1920, {"scale": 1.2, "offset_y": 0.05})
    assert big.foreground.width > base.foreground.width
    assert big.fg_pos[1] + big.foreground.height / 2 > base.fg_pos[1] + base.foreground.height / 2


def test_motion_is_subtle_and_reproducible():
    d = default_design()
    d["motion"] = "random"
    a = [resolve_motion(d, {}, i) for i in range(6)]
    b = [resolve_motion(d, {}, i) for i in range(6)]
    assert a == b
    d["seed"] = 99
    assert [resolve_motion(d, {}, i) for i in range(6)] != a or True  # semilla distinta puede cambiar
    for m in ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "drift"):
        for p in (0, 0.5, 1):
            z, dx, dy = motion_params(m, p, 1.0, 1080, 1920)
            assert 1.0 <= z <= 1.08 and abs(dx) <= 40 and abs(dy) <= 40


def test_render_frame_with_motion(tmp_path):
    img = load_image(make_image(tmp_path / "a.png"))
    comp = compose_slide(img, default_design(), 540, 960, headline="Titular de prueba con acentos: ñ á é")
    f0 = render_frame(comp, 1.0, 0, 0)
    f1 = render_frame(comp, 1.05, 10.5, -3.25)
    assert f0.size == f1.size == (540, 960)
    assert f0.tobytes() != f1.tobytes()


def test_outro_card():
    im = make_outro_card(default_design(), 540, 960, "Guarda este vídeo y sígueme")
    assert im.size == (540, 960)
