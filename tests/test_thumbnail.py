import numpy as np
import pytest
from PIL import Image

from conftest import make_image
from image_processor import scan_folder
from project_manager import ProjectManager, default_design, migrate
from thumbnail import (HEIGHT, MAX_BYTES, SAFE_BAND, WIDTH, ThumbnailError, default_source, default_title,
                       draw_title, make_cover, parse_marked, render_project_thumbnail, save_jpeg)


def test_parse_marked_highlights_words():
    words = parse_marked("Las *5 mejores* apps")
    assert [w.text for w in words] == ["Las", "5", "mejores", "apps"]
    assert [w.accent for w in words] == [False, True, True, False]


@pytest.mark.parametrize("style", ["banda", "imagen_arriba", "solo_texto"])
@pytest.mark.parametrize("tpl", ["tech_dark", "clean_editorial", "high_energy"])
def test_make_cover_size_and_weight(tmp_path, style, tpl):
    src = make_image(tmp_path / "a.png", size=(1080, 1350))
    d = default_design(tpl)
    d["brand_text"] = "@canal"
    img = make_cover(src, d, "5 herramientas de *GitHub* que te ahorran horas", "Gratis", style)
    assert img.size == (WIDTH, HEIGHT)
    out = save_jpeg(img, tmp_path / "m.jpg")
    assert out.stat().st_size < MAX_BYTES
    assert Image.open(out).format == "JPEG"


def test_title_stays_inside_safe_band():
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
    long_title = "Un titular larguísimo con muchísimas palabras para comprobar que nunca se sale de la franja visible"
    top, bottom = draw_title(img, parse_marked(long_title), SAFE_BAND, "", (255, 255, 255), (47, 129, 247))
    assert SAFE_BAND[1] <= top and bottom <= SAFE_BAND[3] + 5
    a = np.array(img.convert("L"))
    rows = np.where(a.max(axis=1) > 0)[0]
    assert rows.min() >= SAFE_BAND[1] - 5 and rows.max() <= SAFE_BAND[3] + 15


def test_empty_title_is_rejected(tmp_path):
    src = make_image(tmp_path / "a.png")
    with pytest.raises(ThumbnailError):
        make_cover(src, default_design(), "   ")


def test_project_thumbnail_defaults(settings, image_folder, tmp_path):
    p = ProjectManager().create("Mi short")
    p.set_source_dir(image_folder, scan_folder(image_folder))
    p.data["slides"][2]["role"] = "cover"
    p.data["slides"][2]["headline"] = "La portada"
    slide = default_source(p)
    assert slide["id"] == p.data["slides"][2]["id"]
    assert default_title(p, slide) == "La portada"
    p.data["thumbnail"]["slide_id"] = p.data["slides"][0]["id"]
    p.data["thumbnail"]["title"] = "Mi *titular*"
    assert default_source(p)["id"] == p.data["slides"][0]["id"]
    out = render_project_thumbnail(p, tmp_path / "x.jpg")
    assert Image.open(out).size == (WIDTH, HEIGHT)


def test_old_projects_get_thumbnail_settings():
    data = migrate({"schema_version": 1, "name": "Viejo", "slides": []})
    assert data["thumbnail"]["style"] == "banda" and data["thumbnail"]["auto"] is True
