"""Miniatura (portada) del Short: imagen 1080×1920 con titular grande, lista para subir a YouTube.

YouTube muestra la miniatura de los Shorts recortada a una zona cercana a 3:2 en la página principal
y en el canal, así que el titular se coloca siempre dentro de la franja central (``SAFE_BAND``).
Las palabras entre asteriscos (``*así*``) se resaltan con el color de acento de la plantilla.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from fonts import get_font
from image_processor import cover_scale, fit_scale, hex_to_rgb, load_image

WIDTH, HEIGHT = 1080, 1920
# Zona que se ve en el recorte ~3:2 (1080×720 centrado) con un pequeño margen de seguridad
SAFE_BAND = (60, 620, WIDTH - 60, HEIGHT - 620)
MAX_BYTES = 2 * 1024 * 1024          # límite clásico de YouTube para miniaturas: siempre por debajo
STYLES = {
    "banda": "Titular en banda central sobre la imagen",
    "imagen_arriba": "Imagen arriba y titular grande debajo",
    "solo_texto": "Sólo titular sobre fondo de la plantilla",
}


class ThumbnailError(Exception):
    pass


def _lum(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def default_thumbnail() -> dict[str, Any]:
    return {"slide_id": "", "title": "", "subtitle": "", "style": "banda", "auto": True}


def default_source(project) -> dict[str, Any] | None:
    slides = project.enabled_slides()
    if not slides:
        return None
    sid = (project.data.get("thumbnail") or {}).get("slide_id")
    for s in slides:
        if s["id"] == sid:
            return s
    return next((s for s in slides if s.get("role") == "cover"), slides[0])


def default_title(project, slide: dict[str, Any] | None) -> str:
    t = (project.data.get("thumbnail") or {}).get("title", "").strip()
    if t:
        return t
    if slide and slide.get("headline", "").strip():
        return slide["headline"].strip()
    return (project.data.get("title") or project.name or "").strip()


# ---------------------------------------------------------------------------
# Texto con resaltado
# ---------------------------------------------------------------------------

@dataclass
class _Word:
    text: str
    accent: bool


def parse_marked(text: str) -> list[_Word]:
    """«Las *5 mejores* apps» → palabras con su marca de resaltado."""
    words: list[_Word] = []
    for i, part in enumerate(re.split(r"\*", text or "")):
        for w in part.split():
            words.append(_Word(w, i % 2 == 1))
    return words


def _wrap(words: list[_Word], font, max_w: float) -> list[list[_Word]]:
    lines: list[list[_Word]] = []
    cur: list[_Word] = []
    for w in words:
        cand = " ".join(x.text for x in cur + [w])
        if cur and font.getlength(cand) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def fit_title(words: list[_Word], font_file: str, box_w: int, box_h: int, max_lines: int = 4,
              start: int = 132, minimum: int = 56):
    """Mayor tamaño de letra con el que el titular cabe en la caja sin partir palabras."""
    size = start
    while size >= minimum:
        font = get_font(font_file, size, bold=True)
        lines = _wrap(words, font, box_w)
        too_wide = any(font.getlength(" ".join(x.text for x in ln)) > box_w for ln in lines)
        asc, desc = font.getmetrics()
        if len(lines) <= max_lines and not too_wide and len(lines) * (asc + desc) * 1.08 <= box_h:
            return font, lines
        size -= 6
    font = get_font(font_file, minimum, bold=True)
    return font, _wrap(words, font, box_w)[:max_lines]


def draw_title(img: Image.Image, words: list[_Word], box: tuple[int, int, int, int], font_file: str,
               color, accent, max_lines: int = 4, stroke_fill=(0, 0, 0)) -> tuple[int, int]:
    """Dibuja el titular centrado en ``box``; devuelve (y_inicio, y_fin) reales del texto."""
    x0, y0, x1, y1 = box
    font, lines = fit_title(words, font_file, x1 - x0, y1 - y0, max_lines)
    d = ImageDraw.Draw(img)
    asc, desc = font.getmetrics()
    lh = (asc + desc) * 1.08
    total = lh * len(lines)
    y = y0 + ((y1 - y0) - total) / 2
    top = y
    stroke = max(3, font.size // 16) if stroke_fill is not None else 0
    space = font.getlength(" ")
    for ln in lines:
        line_w = font.getlength(" ".join(w.text for w in ln))
        x = x0 + ((x1 - x0) - line_w) / 2
        for w in ln:
            d.text((x, y), w.text, font=font, fill=(accent if w.accent else color) + (255,),
                   stroke_width=stroke, stroke_fill=tuple(stroke_fill or (0, 0, 0)) + (255,))
            x += font.getlength(w.text) + space
        y += lh
    return int(top), int(y)


# ---------------------------------------------------------------------------
# Composición
# ---------------------------------------------------------------------------

def _background(src: Image.Image | None, design: dict[str, Any]) -> Image.Image:
    bg_rgb = hex_to_rgb(design.get("bg_color", "#0d1117"))
    if src is None:
        return Image.new("RGBA", (WIDTH, HEIGHT), bg_rgb + (255,))
    s = cover_scale(src.width, src.height, WIDTH, HEIGHT)
    big = src.resize((max(1, int(src.width * s)), max(1, int(src.height * s))), Image.Resampling.LANCZOS)
    big = big.crop(((big.width - WIDTH) // 2, (big.height - HEIGHT) // 2,
                    (big.width - WIDTH) // 2 + WIDTH, (big.height - HEIGHT) // 2 + HEIGHT))
    big = big.filter(ImageFilter.GaussianBlur(28))
    dark = Image.new("RGBA", (WIDTH, HEIGHT), bg_rgb + (int(255 * 0.55),))
    big.alpha_composite(dark)
    return big


def _rounded_paste(base: Image.Image, im: Image.Image, box: tuple[int, int, int, int], radius: int = 28) -> None:
    x0, y0, x1, y1 = box
    s = fit_scale(im.width, im.height, x1 - x0, y1 - y0)
    fg = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.Resampling.LANCZOS)
    mask = Image.new("L", fg.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fg.width - 1, fg.height - 1), radius=radius, fill=255)
    if fg.mode == "RGBA":
        mask = Image.fromarray(np.minimum(np.array(mask), np.array(fg.getchannel("A"))))
    px = x0 + ((x1 - x0) - fg.width) // 2
    py = y0 + ((y1 - y0) - fg.height) // 2
    shadow = Image.new("RGBA", (fg.width + 60, fg.height + 60), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((30, 30, fg.width + 30, fg.height + 30), radius=radius,
                                             fill=(0, 0, 0, 150))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)), (px - 30, py - 18))
    base.paste(fg.convert("RGB"), (px, py), mask)


def make_cover(image_path: str | Path | None, design: dict[str, Any], title: str, subtitle: str = "",
               style: str = "banda", show_safe_zone: bool = False) -> Image.Image:
    if not (title or "").strip():
        raise ThumbnailError("Escribe el titular de la miniatura.")
    src = load_image(image_path) if image_path else None
    if style != "solo_texto" and src is None:
        raise ThumbnailError("Elige la imagen de la miniatura.")
    font_file = design.get("font_file", "")
    text_rgb = hex_to_rgb(design.get("text_color", "#ffffff"))
    accent = hex_to_rgb(design.get("accent_color", "#2f81f7"))
    bg_rgb = hex_to_rgb(design.get("bg_color", "#0d1117"))
    dark = _lum(bg_rgb) < 140
    if dark:     # texto claro con contorno negro
        title_rgb = text_rgb if _lum(text_rgb) > 150 else (255, 255, 255)
        stroke_fill = (0, 0, 0)
    else:        # plantilla clara: texto oscuro limpio, sin contorno
        title_rgb = text_rgb if _lum(text_rgb) < 110 else (17, 17, 17)
        stroke_fill = None
    words = parse_marked(title.strip())
    sx0, sy0, sx1, sy1 = SAFE_BAND

    img = _background(src if style != "solo_texto" else None, design)
    d = ImageDraw.Draw(img)
    if style == "banda":
        _rounded_paste(img, src, (70, 170, WIDTH - 70, HEIGHT - 170))
        band = Image.new("RGBA", (WIDTH, sy1 - sy0 + 40), (0, 0, 0, 0))
        ImageDraw.Draw(band).rectangle((0, 0, WIDTH, band.height), fill=bg_rgb + (248,))
        img.alpha_composite(band, (0, sy0 - 20))
        d.rectangle((0, sy0 - 20, WIDTH, sy0 - 8), fill=accent + (255,))
        d.rectangle((0, sy1 + 8, WIDTH, sy1 + 20), fill=accent + (255,))
        title_box = (sx0 + 10, sy0 + 10, sx1 - 10, sy1 - (90 if subtitle else 10))
    elif style == "imagen_arriba":
        _rounded_paste(img, src, (70, 150, WIDTH - 70, sy0 + 120))
        panel = Image.new("RGBA", (WIDTH, sy1 - sy0 - 100), bg_rgb + (225,))
        img.alpha_composite(panel, (0, sy0 + 140))
        d.rectangle((0, sy0 + 140, WIDTH, sy0 + 152), fill=accent + (255,))
        title_box = (sx0 + 10, sy0 + 165, sx1 - 10, sy1 - (80 if subtitle else 0))
        title_box = (title_box[0], title_box[1], title_box[2], max(title_box[1] + 200, title_box[3]))
    else:
        d.rectangle((sx0, sy0 - 40, sx0 + 160, sy0 - 26), fill=accent + (255,))
        title_box = (sx0, sy0, sx1, sy1 - (90 if subtitle else 0))
    _, text_end = draw_title(img, words, title_box, font_file, title_rgb, accent,
                             max_lines=3 if style == "imagen_arriba" else 4, stroke_fill=stroke_fill)
    if subtitle.strip():
        sfont = get_font(font_file, 54, bold=True)
        sub = subtitle.strip()
        while sfont.getlength(sub) > sx1 - sx0 and len(sub) > 4:
            sub = sub[:-2].rstrip() + "…"
        w = sfont.getlength(sub)
        y = min(text_end + 14, sy1 - 70)
        pad = 22
        d.rounded_rectangle(((WIDTH - w) / 2 - pad, y - 6, (WIDTH + w) / 2 + pad, y + 66), radius=18,
                            fill=accent + (255,))
        d.text(((WIDTH - w) / 2, y), sub, font=sfont, fill=(17, 17, 17, 255) if _lum(accent) > 160 else (255, 255, 255, 255))
    brand = (design.get("brand_text") or "").strip()
    if brand:
        bfont = get_font(font_file, 40, bold=True)
        bw = bfont.getlength(brand)
        by = sy1 + 44
        d.rounded_rectangle(((WIDTH - bw) / 2 - 20, by - 8, (WIDTH + bw) / 2 + 20, by + 56), radius=16,
                            fill=bg_rgb + (235,))
        d.text(((WIDTH - bw) / 2, by), brand, font=bfont, fill=(accent if _lum(accent) < 200 or dark else title_rgb) + (255,))
    if show_safe_zone:
        for y in (HEIGHT // 2 - 360, HEIGHT // 2 + 360):
            for x in range(0, WIDTH, 40):
                d.line((x, y, x + 20, y), fill=(255, 210, 0, 255), width=4)
    return img


def save_jpeg(img: Image.Image, dest: Path) -> Path:
    """Guarda en JPEG por debajo de 2 MB (bajando la calidad si hace falta)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rgb = img.convert("RGB")
    for q in (95, 92, 88, 84, 80, 75, 70):
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=q, optimize=True, progressive=True, subsampling=0 if q >= 88 else 2)
        if buf.tell() <= MAX_BYTES:
            break
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(dest)
    return dest


def render_project_thumbnail(project, dest: Path, show_safe_zone: bool = False) -> Path:
    cfg = {**default_thumbnail(), **(project.data.get("thumbnail") or {})}
    slide = default_source(project)
    if slide is None and cfg["style"] != "solo_texto":
        raise ThumbnailError("El proyecto no tiene imágenes.")
    path = project.image_path(slide["image"]) if slide else None
    img = make_cover(path, project.data["design"], default_title(project, slide), cfg.get("subtitle", ""),
                     cfg.get("style", "banda"), show_safe_zone)
    return save_jpeg(img, dest)
