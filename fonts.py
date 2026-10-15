"""Localización de tipografías.

Orden de búsqueda: archivo elegido por el usuario en ``assets/fonts`` →
tipografías del sistema (Windows, Linux, macOS) → fuente por defecto de Pillow.
Ninguna ruta del equipo se da por supuesta: sólo se usan las que existen.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from config import FONT_EXTENSIONS, get_settings

log = logging.getLogger(__name__)

_WIN_FONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
SYSTEM_CANDIDATES_BOLD = [
    _WIN_FONTS / "segoeuib.ttf", _WIN_FONTS / "arialbd.ttf", _WIN_FONTS / "verdanab.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
SYSTEM_CANDIDATES_REGULAR = [
    _WIN_FONTS / "segoeui.ttf", _WIN_FONTS / "arial.ttf", _WIN_FONTS / "verdana.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
]


def list_user_fonts() -> list[str]:
    d = get_settings().fonts_path
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.suffix.lower() in FONT_EXTENSIONS)


def resolve_font_path(font_file: str = "", bold: bool = True) -> Path | None:
    """Devuelve la ruta de la fuente a usar, o ``None`` si no hay ninguna TTF/OTF disponible."""
    if font_file:
        p = Path(font_file)
        if not p.is_absolute():
            p = get_settings().fonts_path / font_file
        if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS:
            return p
        log.warning("No se encuentra la fuente %s; se usa una del sistema.", font_file)
    for cand in (SYSTEM_CANDIDATES_BOLD if bold else SYSTEM_CANDIDATES_REGULAR):
        if cand.is_file():
            return cand
    for cand in (SYSTEM_CANDIDATES_REGULAR if bold else SYSTEM_CANDIDATES_BOLD):
        if cand.is_file():
            return cand
    return None


@lru_cache(maxsize=64)
def _load(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow antiguo
        return ImageFont.load_default()


def get_font(font_file: str = "", size: int = 48, bold: bool = True):
    p = resolve_font_path(font_file, bold)
    return _load(str(p) if p else "", max(8, int(size)))


def font_family_name(path: Path | None) -> str:
    """Nombre de familia que libass debe usar para el estilo ASS."""
    if path is None:
        return "Arial"
    try:
        fam, _style = ImageFont.truetype(str(path), 20).getname()
        return fam or "Arial"
    except OSError:
        return "Arial"


def supports_spanish(path: Path | None) -> bool:
    """Comprueba que la fuente tenga glifos para acentos y signos españoles."""
    if path is None:
        return False
    try:
        font = ImageFont.truetype(str(path), 32)
        for ch in "áéíóúüñÁÉÍÓÚÜÑ¿¡":
            if font.getmask(ch).getbbox() is None:
                return False
        return True
    except OSError:
        return False
