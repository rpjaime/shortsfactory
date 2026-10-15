"""Plantillas visuales de ShortsFactory.

Cada plantilla define valores de diseño que el usuario puede personalizar en la
pestaña *Diseño*. Los valores personalizados se guardan en el proyecto; la
plantilla sólo aporta los valores de partida.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {
    "tech_dark": {
        "label": "Tech Dark",
        "description": "Fondo oscuro, acentos de color y subtítulos muy legibles. Pensada para GitHub, IA y desarrollo.",
        "values": {
            "bg_color": "#0d1117",
            "accent_color": "#2f81f7",
            "text_color": "#ffffff",
            "subtitle_color": "#ffffff",
            "subtitle_outline_color": "#000000",
            "subtitle_box": True,
            "subtitle_box_opacity": 0.55,
            "subtitle_size": 62,
            "title_size": 64,
            "font_weight_bold": True,
            "composition": "blur",
            "blur_darken": 0.55,
            "motion": "random",
            "motion_intensity": 0.5,
            "transition": "fade",
            "transition_duration": 0.45,
            "cover_duration": 2.5,
            "image_shadow": True,
            "image_radius": 18,
            "headline_style": "bar",
        },
    },
    "clean_editorial": {
        "label": "Clean Editorial",
        "description": "Fondo neutro, tipografía limpia y pocos efectos. Prioriza la lectura de las capturas.",
        "values": {
            "bg_color": "#f4f1ea",
            "accent_color": "#1f2937",
            "text_color": "#111111",
            "subtitle_color": "#111111",
            "subtitle_outline_color": "#ffffff",
            "subtitle_box": True,
            "subtitle_box_opacity": 0.75,
            "subtitle_size": 58,
            "title_size": 58,
            "font_weight_bold": False,
            "composition": "solid",
            "blur_darken": 0.35,
            "motion": "zoom_in",
            "motion_intensity": 0.25,
            "transition": "fade",
            "transition_duration": 0.5,
            "cover_duration": 2.5,
            "image_shadow": False,
            "image_radius": 6,
            "headline_style": "plain",
        },
    },
    "high_energy": {
        "label": "High Energy",
        "description": "Más ritmo visual, transiciones algo más rápidas y titulares destacados, sin efectos que distraigan.",
        "values": {
            "bg_color": "#111111",
            "accent_color": "#ffcc00",
            "text_color": "#ffffff",
            "subtitle_color": "#ffffff",
            "subtitle_outline_color": "#000000",
            "subtitle_box": False,
            "subtitle_box_opacity": 0.5,
            "subtitle_size": 70,
            "title_size": 76,
            "font_weight_bold": True,
            "composition": "blur",
            "blur_darken": 0.5,
            "motion": "random",
            "motion_intensity": 0.75,
            "transition": "slideleft",
            "transition_duration": 0.3,
            "cover_duration": 2.0,
            "image_shadow": True,
            "image_radius": 24,
            "headline_style": "highlight",
        },
    },
}

DEFAULT_TEMPLATE = "tech_dark"

# Transiciones disponibles (todas sutiles; nada de destellos)
TRANSITIONS: dict[str, str] = {
    "none": "Corte directo",
    "fade": "Fundido cruzado",
    "fadeblack": "Fundido a negro",
    "slideleft": "Deslizar a la izquierda",
    "slideup": "Deslizar hacia arriba",
    "smoothleft": "Barrido suave a la izquierda",
    "smoothup": "Barrido suave hacia arriba",
}

MOTIONS: dict[str, str] = {
    "none": "Sin movimiento",
    "zoom_in": "Zoom lento de entrada",
    "zoom_out": "Zoom de salida",
    "pan_left": "Desplazamiento horizontal (izquierda)",
    "pan_right": "Desplazamiento horizontal (derecha)",
    "pan_up": "Desplazamiento vertical (arriba)",
    "pan_down": "Desplazamiento vertical (abajo)",
    "drift": "Movimiento suave de cámara",
    "random": "Alternado aleatorio (semilla reproducible)",
}
RANDOM_MOTION_POOL = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "drift")

COMPOSITIONS: dict[str, str] = {
    "blur": "A · Imagen completa con fondo ampliado y desenfocado",
    "solid": "B · Imagen completa sobre fondo sólido",
    "fill": "C · Ampliar para llenar (sólo si el recorte es aceptable)",
}


def template_values(name: str) -> dict[str, Any]:
    tpl = TEMPLATES.get(name) or TEMPLATES[DEFAULT_TEMPLATE]
    return deepcopy(tpl["values"])


def template_choices() -> list[tuple[str, str]]:
    return [(t["label"], key) for key, t in TEMPLATES.items()]
