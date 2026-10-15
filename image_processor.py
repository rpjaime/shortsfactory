"""Carga, ordenación, análisis y composición de imágenes.

* Ordenación natural ("2.png" antes que "10.png").
* Detección de imágenes ilegibles, pequeñas y duplicadas (exactas y casi exactas).
* Lectura de la orientación EXIF y compatibilidad con transparencias.
* Composición vertical 9:16 en tres modos (A desenfoque, B fondo sólido, C llenar).
* Animación sutil por fotograma con transformaciones afines con precisión subpíxel.
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from config import IMAGE_EXTENSIONS
from fonts import get_font
from templates import RANDOM_MOTION_POOL

log = logging.getLogger(__name__)

MIN_RECOMMENDED_SIDE = 720       # por debajo se avisa de que puede verse borrosa
MAX_ACCEPTABLE_CROP = 0.22       # modo C: fracción máxima de imagen que puede perderse
NEAR_DUPLICATE_DISTANCE = 12     # distancia de Hamming del dHash de 256 bits

Image.MAX_IMAGE_PIXELS = 200_000_000  # evita bombas de descompresión absurdas


# ---------------------------------------------------------------------------
# Ordenación y análisis
# ---------------------------------------------------------------------------

def natural_key(name: str) -> list[Any]:
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def scan_folder(folder: str | Path) -> list[str]:
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {folder}")
    names = [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
             and not p.name.startswith(".")]
    return sorted(names, key=natural_key)


@dataclass
class ImageInfo:
    name: str
    path: str
    ok: bool
    width: int = 0
    height: int = 0
    aspect: float = 0.0
    mode: str = ""
    has_alpha: bool = False
    exif_rotated: bool = False
    sha256: str = ""
    dhash: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def aspect_label(self) -> str:
        if not self.ok or not self.height:
            return "-"
        known = {(1, 1): "1:1", (4, 5): "4:5", (9, 16): "9:16", (16, 9): "16:9", (3, 4): "3:4", (4, 3): "4:3"}
        g = math.gcd(self.width, self.height)
        w, h = self.width // g, self.height // g
        if (w, h) in known:
            return known[(w, h)]
        return f"{self.aspect:.2f}:1"


def _dhash(img: Image.Image, size: int = 16) -> int:
    g = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def analyze_image(path: str | Path) -> ImageInfo:
    path = Path(path)
    info = ImageInfo(name=path.name, path=str(path), ok=False)
    try:
        data = path.read_bytes()
        info.sha256 = hashlib.sha256(data).hexdigest()
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            orientation = im.getexif().get(0x0112, 1)
            im = ImageOps.exif_transpose(im)
            im.load()
            info.exif_rotated = orientation not in (1, None)
            info.width, info.height = im.size
            info.mode = im.mode
            info.has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            info.aspect = info.width / info.height if info.height else 0
            info.dhash = _dhash(im)
        info.ok = True
        if min(info.width, info.height) < MIN_RECOMMENDED_SIDE:
            info.warnings.append(
                f"Imagen pequeña ({info.width}×{info.height}); puede verse borrosa en 1080×1920.")
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de lectura = imagen ilegible
        info.error = f"Imagen ilegible: {exc}"
    return info


def analyze_folder(folder: str | Path, names: list[str] | None = None) -> list[ImageInfo]:
    folder = Path(folder)
    names = names if names is not None else scan_folder(folder)
    infos = [analyze_image(folder / n) for n in names]
    mark_duplicates(infos)
    return infos


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def mark_duplicates(infos: list[ImageInfo]) -> list[tuple[str, str, str]]:
    """Añade avisos de duplicados. Devuelve [(a, b, tipo)]."""
    pairs = []
    ok = [i for i in infos if i.ok]
    for i, a in enumerate(ok):
        for b in ok[i + 1:]:
            if a.sha256 == b.sha256:
                kind = "idéntica"
            elif hamming(a.dhash, b.dhash) <= NEAR_DUPLICATE_DISTANCE and abs(a.aspect - b.aspect) < 0.02:
                kind = "casi idéntica"
            else:
                continue
            pairs.append((a.name, b.name, kind))
            b.warnings.append(f"Posible duplicado ({kind}) de {a.name}.")
    return pairs


def load_image(path: str | Path) -> Image.Image:
    """Abre una imagen respetando EXIF y la devuelve en RGBA."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "P" and "transparency" in im.info:
            im = im.convert("RGBA")
        return im.convert("RGBA")


def make_thumbnail(src: str | Path, dest: Path, size: int = 320) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    im = load_image(src)
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    bg = Image.new("RGBA", im.size, (40, 40, 40, 255))
    bg.alpha_composite(im)
    bg.convert("RGB").save(dest, "JPEG", quality=85)
    return dest


# ---------------------------------------------------------------------------
# Geometría de la composición
# ---------------------------------------------------------------------------

def hex_to_rgb(value: str, default=(0, 0, 0)) -> tuple[int, int, int]:
    v = (value or "").strip().lstrip("#")
    if v.startswith("rgb"):
        nums = re.findall(r"[\d.]+", v)
        if len(nums) >= 3:
            return tuple(int(float(n)) for n in nums[:3])  # type: ignore[return-value]
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        return default


@dataclass
class Layout:
    width: int
    height: int
    k: float                      # factor de escala respecto a 1080 de ancho
    headline_box: tuple[int, int, int, int]
    image_box: tuple[int, int, int, int]
    subtitle_center_y: int
    subtitle_box: tuple[int, int, int, int]
    brand_pos: tuple[int, int]


def compute_layout(design: dict[str, Any], width: int, height: int) -> Layout:
    k = width / 1080.0
    sub_size = design.get("subtitle_size", 62) * k
    lines = max(1, int(design.get("subtitle_max_lines", 2)))
    band_h = int(lines * sub_size * 1.3 + 40 * k)
    pos = float(design.get("subtitle_position", 0.30))
    pos = min(0.6, max(0.12, pos))
    cy = int(height * (1.0 - pos))
    sub_top = cy - band_h // 2
    sub_bottom = cy + band_h // 2
    top = int(150 * k)                                # zona superior reservada a la interfaz de YouTube
    headline_h = int(design.get("title_size", 64) * k * 2.6) if design.get("show_headlines", True) else 0
    head_box = (int(60 * k), top, width - int(60 * k), top + headline_h)
    img_top = top + headline_h + int(25 * k)
    img_bottom = sub_top - int(30 * k)
    margin_x = int(45 * k)
    if img_bottom - img_top < height * 0.3:          # subtítulos demasiado altos: no aplastar la imagen
        img_bottom = img_top + int(height * 0.3)
    image_box = (margin_x, img_top, width - margin_x, img_bottom)
    mx = int(design.get("subtitle_margin_x", 70) * k)
    return Layout(width, height, k, head_box, image_box, cy, (mx, sub_top, width - mx, sub_bottom),
                  (int(60 * k), int(95 * k)))


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def wrap_text(text: str, font, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if font.getlength(cand) <= max_width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_text_block(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], font,
                    fill, align: str = "center", max_lines: int = 3, line_spacing: float = 1.15,
                    stroke_width: int = 0, stroke_fill=None, valign: str = "center") -> None:
    x0, y0, x1, y1 = box
    lines = [l for para in text.replace("\r", "").split("\n") for l in wrap_text(para, font, x1 - x0)][:max_lines]
    if not lines:
        return
    asc, desc = font.getmetrics() if hasattr(font, "getmetrics") else (20, 5)
    lh = (asc + desc) * line_spacing
    total = lh * len(lines)
    y = y0 + ((y1 - y0) - total) / 2 if valign == "center" else y0
    for line in lines:
        w = font.getlength(line)
        x = x0 + ((x1 - x0) - w) / 2 if align == "center" else x0
        draw.text((x, y), line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        y += lh


# ---------------------------------------------------------------------------
# Composición
# ---------------------------------------------------------------------------

@dataclass
class Composition:
    """Capas de una diapositiva lista para animar."""

    background: Image.Image          # RGBA, tamaño del lienzo (fondo + titular + marca)
    foreground: Image.Image          # RGBA, imagen (con sombra) en su tamaño colocado
    fg_pos: tuple[float, float]      # esquina superior izquierda en el lienzo con zoom 1
    mode: str                        # blur | solid | fill
    warnings: list[str] = field(default_factory=list)


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    if radius <= 0:
        return im
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius=radius, fill=255)
    out = im.copy()
    alpha = ImageChops.multiply(out.getchannel("A"), mask)
    out.putalpha(alpha)
    return out


def _with_shadow(im: Image.Image, k: float) -> tuple[Image.Image, int]:
    pad = max(4, int(28 * k))
    canvas = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 150))
    shadow.putalpha(ImageChops.multiply(im.getchannel("A"), Image.new("L", im.size, 150)))
    canvas.alpha_composite(shadow, (pad, pad + int(8 * k)))
    canvas = canvas.filter(ImageFilter.GaussianBlur(max(2, int(14 * k))))
    canvas.alpha_composite(im, (pad, pad))
    return canvas, pad


def cover_scale(src_w: int, src_h: int, dst_w: int, dst_h: int) -> float:
    return max(dst_w / src_w, dst_h / src_h)


def fit_scale(src_w: int, src_h: int, dst_w: int, dst_h: int) -> float:
    return min(dst_w / src_w, dst_h / src_h)


def crop_fraction_for_fill(src_w: int, src_h: int, dst_w: int, dst_h: int) -> float:
    """Fracción del área de la imagen que se perdería al llenar el lienzo."""
    s = cover_scale(src_w, src_h, dst_w, dst_h)
    return 1.0 - (dst_w * dst_h) / (src_w * s * src_h * s)


def effective_overrides(slide: dict[str, Any] | None) -> dict[str, Any]:
    ov = dict((slide or {}).get("overrides") or {})
    return ov


def compose_slide(image: Image.Image, design: dict[str, Any], width: int, height: int,
                  overrides: dict[str, Any] | None = None, headline: str = "") -> Composition:
    ov = overrides or {}
    lay = compute_layout(design, width, height)
    k = lay.k
    warnings: list[str] = []
    mode = ov.get("composition") or design.get("composition", "blur")
    bg_rgb = hex_to_rgb(design.get("bg_color", "#000000"))

    if mode == "fill":
        frac = crop_fraction_for_fill(image.width, image.height, width, height)
        if frac > MAX_ACCEPTABLE_CROP and not ov.get("force_fill"):
            warnings.append(
                f"El modo «llenar» recortaría el {frac:.0%} de la imagen; se usa el modo con fondo desenfocado "
                "para no perder contenido. Puedes forzarlo en los ajustes de la diapositiva.")
            mode = "blur"

    background = Image.new("RGBA", (width, height), bg_rgb + (255,))
    if mode in ("blur", "fill"):
        small_w = max(16, width // 8)
        small_h = max(16, height // 8)
        s = cover_scale(image.width, image.height, small_w, small_h)
        small = image.resize((max(1, round(image.width * s)), max(1, round(image.height * s))),
                             Image.Resampling.BILINEAR)
        left = (small.width - small_w) // 2
        top = (small.height - small_h) // 2
        small = small.crop((left, top, left + small_w, top + small_h))
        flat = Image.new("RGBA", small.size, bg_rgb + (255,))
        flat.alpha_composite(small)
        blurred = flat.filter(ImageFilter.GaussianBlur(6)).resize((width, height), Image.Resampling.BICUBIC)
        blurred = blurred.filter(ImageFilter.GaussianBlur(max(2, int(10 * k))))
        darken = float(design.get("blur_darken", 0.5))
        background = Image.blend(blurred, Image.new("RGBA", (width, height), (0, 0, 0, 255)), darken)

    draw = ImageDraw.Draw(background)
    text_rgb = hex_to_rgb(design.get("text_color", "#ffffff"))
    accent_rgb = hex_to_rgb(design.get("accent_color", "#2f81f7"))
    font_file = design.get("font_file", "")
    bold = bool(design.get("font_weight_bold", True))

    if headline and design.get("show_headlines", True) and mode != "fill":
        hb = lay.headline_box
        font = get_font(font_file, int(design.get("title_size", 64) * k), bold=True)
        style = design.get("headline_style", "plain")
        if style == "bar":
            draw.rounded_rectangle((hb[0], hb[3] - int(10 * k), hb[0] + int(120 * k), hb[3] - int(2 * k)),
                                   radius=int(4 * k), fill=accent_rgb + (255,))
            draw_text_block(draw, headline, (hb[0], hb[1], hb[2], hb[3] - int(14 * k)), font, text_rgb + (255,),
                            align="left", max_lines=2)
        elif style == "highlight":
            lines = wrap_text(headline, font, hb[2] - hb[0] - int(40 * k))[:2]
            asc, desc = font.getmetrics()
            lh = (asc + desc) * 1.2
            y = hb[1] + ((hb[3] - hb[1]) - lh * len(lines)) / 2
            for line in lines:
                w = font.getlength(line)
                x = (width - w) / 2
                draw.rounded_rectangle((x - 18 * k, y - 4 * k, x + w + 18 * k, y + lh - 6 * k), radius=int(10 * k),
                                       fill=accent_rgb + (255,))
                dark = (17, 17, 17) if sum(accent_rgb) > 380 else (255, 255, 255)
                draw.text((x, y), line, font=font, fill=dark + (255,))
                y += lh
        else:
            draw_text_block(draw, headline, hb, font, text_rgb + (255,), max_lines=2)

    brand = (design.get("brand_text") or "").strip()
    if brand:
        bfont = get_font(font_file, int(34 * k), bold=bold)
        bx, by = lay.brand_pos
        draw.text((bx, by - int(40 * k)), brand, font=bfont, fill=text_rgb + (200,),
                  stroke_width=max(1, int(2 * k)), stroke_fill=(0, 0, 0, 160))

    # --- capa de imagen ---
    scale_mul = float(ov.get("scale", 1.0) or 1.0)
    off_x = float(ov.get("offset_x", 0.0) or 0.0) * width
    off_y = float(ov.get("offset_y", 0.0) or 0.0) * height
    if mode == "fill":
        s = cover_scale(image.width, image.height, width, height) * max(1.0, scale_mul)
        fw, fh = max(1, round(image.width * s)), max(1, round(image.height * s))
        fg = image.resize((fw, fh), Image.Resampling.LANCZOS)
        fx = float(ov.get("focus_x", 0.5))
        fy = float(ov.get("focus_y", 0.5))
        px = -(fw - width) * min(1.0, max(0.0, fx))
        py = -(fh - height) * min(1.0, max(0.0, fy))
        flat = Image.new("RGBA", fg.size, bg_rgb + (255,))
        flat.alpha_composite(fg)
        return Composition(background, flat, (px, py), mode, warnings)

    x0, y0, x1, y1 = lay.image_box
    s = fit_scale(image.width, image.height, x1 - x0, y1 - y0) * scale_mul
    fw, fh = max(1, round(image.width * s)), max(1, round(image.height * s))
    if s > 1.6:
        warnings.append("La imagen se amplía más de un 60 %: puede perder nitidez.")
    fg = image.resize((fw, fh), Image.Resampling.LANCZOS)
    if mode == "solid" and image.mode == "RGBA" and image.getchannel("A").getextrema()[0] < 255:
        pass  # la transparencia se respeta: se ve el color de fondo
    fg = _rounded(fg, int(design.get("image_radius", 0) * k))
    pos_x = x0 + ((x1 - x0) - fw) / 2 + off_x
    pos_y = y0 + ((y1 - y0) - fh) / 2 + off_y
    if design.get("image_shadow", False):
        fg, pad = _with_shadow(fg, k)
        pos_x -= pad
        pos_y -= pad
    return Composition(background, fg, (pos_x, pos_y), mode, warnings)


def make_outro_card(design: dict[str, Any], width: int, height: int, text: str) -> Image.Image:
    """Tarjeta de cierre generada por la plantilla (sin marca de agua obligatoria)."""
    k = width / 1080.0
    bg_rgb = hex_to_rgb(design.get("bg_color", "#000000"))
    accent = hex_to_rgb(design.get("accent_color", "#2f81f7"))
    text_rgb = hex_to_rgb(design.get("text_color", "#ffffff"))
    im = Image.new("RGBA", (width, height), bg_rgb + (255,))
    d = ImageDraw.Draw(im)
    lay = compute_layout(design, width, height)
    box = (int(90 * k), lay.image_box[1], width - int(90 * k), lay.image_box[3])
    d.rounded_rectangle(box, radius=int(36 * k), outline=accent + (255,), width=max(2, int(6 * k)))
    font = get_font(design.get("font_file", ""), int(design.get("title_size", 64) * k * 1.05), bold=True)
    draw_text_block(d, text or "¡Gracias por ver!", (box[0] + int(50 * k), box[1], box[2] - int(50 * k), box[3]),
                    font, text_rgb + (255,), max_lines=5)
    brand = (design.get("brand_text") or "").strip()
    if brand:   # dentro de la tarjeta, para que nunca lo tape el subtítulo
        bfont = get_font(design.get("font_file", ""), int(46 * k), bold=True)
        w = bfont.getlength(brand)
        d.text(((width - w) / 2, box[3] - int(110 * k)), brand, font=bfont, fill=accent + (255,))
    return im


# ---------------------------------------------------------------------------
# Movimiento
# ---------------------------------------------------------------------------

def resolve_motion(design: dict[str, Any], overrides: dict[str, Any] | None, index: int) -> str:
    m = (overrides or {}).get("motion") or design.get("motion", "none")
    if m == "random":
        rng = random.Random(int(design.get("seed", 0)) * 1000003 + index)
        m = rng.choice(RANDOM_MOTION_POOL)
    return m


def _ease(p: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * min(1.0, max(0.0, p)))


def motion_params(motion: str, p: float, intensity: float, width: int, height: int) -> tuple[float, float, float]:
    """Devuelve (zoom, dx, dy) para el progreso ``p`` ∈ [0, 1]. Movimiento sutil."""
    e = _ease(p)
    inten = min(1.0, max(0.0, intensity))
    zmax = 0.07 * inten                # como mucho un 7 % de zoom
    pan = 0.035 * inten * width        # como mucho ~38 px a 1080
    if motion == "zoom_in":
        return 1.0 + zmax * e, 0.0, 0.0
    if motion == "zoom_out":
        return 1.0 + zmax * (1 - e), 0.0, 0.0
    if motion == "pan_left":
        return 1.0 + zmax * 0.3, pan * (0.5 - e), 0.0
    if motion == "pan_right":
        return 1.0 + zmax * 0.3, pan * (e - 0.5), 0.0
    if motion == "pan_up":
        return 1.0 + zmax * 0.3, 0.0, pan * (0.5 - e)
    if motion == "pan_down":
        return 1.0 + zmax * 0.3, 0.0, pan * (e - 0.5)
    if motion == "drift":
        return 1.0 + zmax * 0.6 * e, pan * 0.6 * (e - 0.5), pan * 0.4 * math.sin(math.pi * e) * 0.5
    return 1.0, 0.0, 0.0


def render_frame(comp: Composition, zoom: float, dx: float, dy: float) -> Image.Image:
    """Compone un fotograma RGB aplicando zoom y desplazamiento con precisión subpíxel."""
    frame = comp.background.copy()
    fg = comp.foreground
    W, H = frame.size
    if comp.mode == "fill":
        zoom = max(zoom, 1.0)
        if abs(dx) > 0 or abs(dy) > 0:   # en modo llenar, el desplazamiento necesita margen de zoom
            need = 1.0 + 2 * max(abs(dx) / W, abs(dy) / H)
            zoom = max(zoom, need)
    fw, fh = fg.size
    cx = comp.fg_pos[0] + fw / 2 + dx
    cy = comp.fg_pos[1] + fh / 2 + dy
    if abs(zoom - 1.0) < 1e-6 and float(cx - fw / 2).is_integer() and float(cy - fh / 2).is_integer():
        _paste_clipped(frame, fg, int(cx - fw / 2), int(cy - fh / 2))
        return frame.convert("RGB")
    left = math.floor(cx - fw * zoom / 2)
    top = math.floor(cy - fh * zoom / 2)
    right = math.ceil(cx + fw * zoom / 2)
    bottom = math.ceil(cy + fh * zoom / 2)
    ox, oy = max(0, left), max(0, top)
    ex, ey = min(W, right), min(H, bottom)
    if ex <= ox or ey <= oy:
        return frame.convert("RGB")
    rw, rh = ex - ox, ey - oy
    inv = 1.0 / zoom
    a, b, c = inv, 0.0, (ox - cx) * inv + fw / 2
    d, e, f = 0.0, inv, (oy - cy) * inv + fh / 2
    region = fg.transform((rw, rh), Image.Transform.AFFINE, (a, b, c, d, e, f),
                          resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    frame.alpha_composite(region, (ox, oy))
    return frame.convert("RGB")


def _paste_clipped(frame: Image.Image, fg: Image.Image, x: int, y: int) -> None:
    W, H = frame.size
    sx, sy = max(0, -x), max(0, -y)
    dx, dy = max(0, x), max(0, y)
    w = min(fg.width - sx, W - dx)
    h = min(fg.height - sy, H - dy)
    if w > 0 and h > 0:
        frame.alpha_composite(fg.crop((sx, sy, sx + w, sy + h)), (dx, dy))
