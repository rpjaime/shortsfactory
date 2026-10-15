"""Creación, lectura, guardado y gestión de proyectos.

Cada proyecto vive en su propio directorio::

    projects/<id>/
        project.json        manifiesto versionado (schema_version)
        project.json.bak    copia del último guardado correcto (recuperación)
        audio/              narración generada (caché por contenido)
        subtitles/          ASS/SRT generados
        cache/              miniaturas, fotogramas compuestos y clips reutilizables
        exports/            guiones exportados

Las imágenes originales NO se copian: el manifiesto guarda la carpeta de origen
y el nombre de cada archivo, y toda referencia se valida para que no pueda salir
de esa carpeta.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_VOICE_ID, DEFAULT_WIDTH, IMAGE_EXTENSIONS, get_settings
from templates import DEFAULT_TEMPLATE, template_values

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST = "project.json"
SLIDE_ROLES = ("hook", "content", "transition", "cta", "silent")


class ProjectError(Exception):
    """Error comprensible relacionado con un proyecto."""


class PathSecurityError(ProjectError):
    """Una referencia intenta salir de la carpeta permitida."""


# ---------------------------------------------------------------------------
# Valores predeterminados
# ---------------------------------------------------------------------------

def default_design(template: str = DEFAULT_TEMPLATE) -> dict[str, Any]:
    d = {
        "template": template,
        "font_file": "",            # vacío = fuente del sistema
        "subtitle_position": 0.30,  # fracción de la altura medida desde abajo (centro del bloque)
        "subtitle_max_chars": 26,
        "subtitle_max_lines": 2,
        "subtitle_margin_x": 70,
        "brand_text": "",
        "seed": 1234,
        "min_slide_duration": 2.0,
        "show_headlines": True,
        "outro_mode": "card",       # card | last_slide
        "video_fade": 0.35,         # fundido de entrada/salida del vídeo completo
    }
    d.update(template_values(template))
    return d


def default_project(name: str) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "",
        "name": name,
        "title": name,
        "language": "es-ES",
        "created_at": now,
        "updated_at": now,
        "source_dir": "",
        "slides": [],
        "outro": {"enabled": True, "narration": "", "text": ""},
        "thumbnail": {"slide_id": "", "title": "", "subtitle": "", "style": "banda", "auto": True},
        "voice": {
            "engine": "piper",
            "voice_id": DEFAULT_VOICE_ID,
            "speaker_id": None,
            "speed": 1.0,              # 1.0 = ritmo natural (Piper length_scale = 1/speed)
            "sentence_pause": 0.25,
            "noise_scale": None,
            "noise_w_scale": None,
        },
        "design": default_design(),
        "timing": {
            "target_duration": 40.0,
            "lead_margin": 0.15,       # margen visual antes de cada frase
            "pause_between": 0.45,     # pausa tras cada fragmento (antes de la transición)
            "fill_to_target": False,   # alargar diapositivas hasta la duración objetivo
            "silent_slide_duration": 2.5,
        },
        "audio": {
            "music_file": "",
            "music_volume": 0.18,
            "voice_volume": 1.0,
            "ducking": True,
            "ducking_strength": 0.6,
            "fade_in": 1.0,
            "fade_out": 1.5,
        },
        "export": {
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "fps": DEFAULT_FPS,
            "crf": 20,
            "preset": "medium",
            "audio_bitrate": "192k",
        },
        "llm_notes": {"topic": "", "extra": ""},
        "last_export": {},
    }


def default_slide(image: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "image": image,
        "enabled": True,
        "role": "content",
        "headline": "",
        "narration": "",
        "notes": "",            # información aportada por el usuario para la IA
        "overrides": {},        # composition, scale, offset_x, offset_y, focus_x, focus_y, motion
    }


# ---------------------------------------------------------------------------
# Utilidades de rutas
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return (norm or "proyecto")[:max_len].strip("-") or "proyecto"


def safe_join(base: Path, relative: str) -> Path:
    """Une ``relative`` a ``base`` garantizando que el resultado queda dentro de ``base``.

    Rechaza rutas absolutas, unidades, ``..`` y separadores manipulados.
    """
    if relative is None or not str(relative).strip():
        raise PathSecurityError("Referencia de archivo vacía.")
    rel = str(relative).strip()
    if "\x00" in rel:
        raise PathSecurityError("La referencia contiene caracteres no válidos.")
    if os.path.isabs(rel) or re.match(r"^[a-zA-Z]:", rel) or rel.startswith(("\\", "/")):
        raise PathSecurityError(f"No se permiten rutas absolutas: {rel!r}")
    parts = re.split(r"[\\/]+", rel)
    if any(p in ("..",) for p in parts):
        raise PathSecurityError(f"No se permite salir de la carpeta del proyecto: {rel!r}")
    base_r = Path(base).resolve()
    target = (base_r / Path(*parts)).resolve()
    try:
        target.relative_to(base_r)
    except ValueError as exc:
        raise PathSecurityError(f"La referencia {rel!r} apunta fuera de {base_r}") from exc
    return target


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Migraciones
# ---------------------------------------------------------------------------

def _deep_fill(target: dict, defaults: dict) -> dict:
    """Añade a ``target`` las claves que falten en ``defaults`` (recursivo)."""
    for k, v in defaults.items():
        if k not in target:
            target[k] = deepcopy(v)
        elif isinstance(v, dict) and isinstance(target.get(k), dict) and k not in ("overrides", "last_export"):
            _deep_fill(target[k], v)
    return target


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Lleva un manifiesto de cualquier versión conocida a ``SCHEMA_VERSION``."""
    if not isinstance(data, dict):
        raise ProjectError("El manifiesto del proyecto no es un objeto JSON.")
    version = data.get("schema_version", 0)
    if not isinstance(version, int) or version < 0:
        raise ProjectError(f"schema_version no válido: {version!r}")
    if version > SCHEMA_VERSION:
        raise ProjectError(
            f"El proyecto usa schema_version {version}, más nuevo que el soportado ({SCHEMA_VERSION}). "
            "Actualiza ShortsFactory.")
    if version == 0:
        # Versión 0: borradores sin versión; las diapositivas podían ser cadenas.
        slides = []
        for s in data.get("slides", []):
            if isinstance(s, str):
                slides.append(default_slide(s))
            elif isinstance(s, dict):
                slides.append(s)
        data["slides"] = slides
        data["schema_version"] = 1
    defaults = default_project(data.get("name") or "Proyecto")
    _deep_fill(data, defaults)
    for s in data["slides"]:
        _deep_fill(s, default_slide(s.get("image", "")))
        if s.get("role") not in SLIDE_ROLES:
            s["role"] = "content"
    return data


# ---------------------------------------------------------------------------
# Proyecto
# ---------------------------------------------------------------------------

class Project:
    """Envoltorio de un manifiesto de proyecto con guardado atómico."""

    def __init__(self, directory: Path, data: dict[str, Any]):
        self.dir = Path(directory)
        self.data = data
        self._lock = threading.RLock()

    # -- rutas --
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST

    @property
    def audio_dir(self) -> Path:
        return self.dir / "audio"

    @property
    def subtitles_dir(self) -> Path:
        return self.dir / "subtitles"

    @property
    def cache_dir(self) -> Path:
        return self.dir / "cache"

    @property
    def exports_dir(self) -> Path:
        return self.dir / "exports"

    @property
    def source_dir(self) -> Path | None:
        sd = self.data.get("source_dir") or ""
        return Path(sd) if sd else None

    def ensure_dirs(self) -> None:
        for d in (self.audio_dir, self.subtitles_dir, self.cache_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- persistencia --
    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if self.manifest_path.exists():
                try:
                    json.loads(self.manifest_path.read_text(encoding="utf-8"))
                    shutil.copy2(self.manifest_path, self.manifest_path.with_suffix(".json.bak"))
                except (OSError, json.JSONDecodeError):
                    log.warning("El manifiesto actual estaba dañado; no se sobrescribe la copia de seguridad.")
            atomic_write_json(self.manifest_path, self.data)

    # -- imágenes --
    def image_path(self, image_name: str) -> Path:
        src = self.source_dir
        if src is None:
            raise ProjectError("El proyecto no tiene carpeta de imágenes asignada.")
        return safe_join(src, image_name)

    def slide_by_id(self, slide_id: str) -> dict[str, Any] | None:
        return next((s for s in self.data["slides"] if s["id"] == slide_id), None)

    def enabled_slides(self) -> list[dict[str, Any]]:
        return [s for s in self.data["slides"] if s.get("enabled", True)]

    def set_source_dir(self, folder: str | Path, image_names: list[str]) -> None:
        """Asigna la carpeta de imágenes conservando el texto de diapositivas existentes."""
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ProjectError(f"La carpeta no existe: {folder}")
        existing = {s["image"]: s for s in self.data["slides"]}
        new_slides = []
        for name in image_names:
            safe_join(folder, name)  # valida
            new_slides.append(existing.get(name) or default_slide(name))
        self.data["source_dir"] = str(folder)
        self.data["slides"] = new_slides

    def move_slide(self, index: int, delta: int) -> int:
        slides = self.data["slides"]
        if not 0 <= index < len(slides):
            raise ProjectError("Índice de diapositiva fuera de rango.")
        new = max(0, min(len(slides) - 1, index + delta))
        slides.insert(new, slides.pop(index))
        return new

    def move_slide_to(self, index: int, new_index: int) -> int:
        slides = self.data["slides"]
        if not 0 <= index < len(slides):
            raise ProjectError("Índice de diapositiva fuera de rango.")
        new = max(0, min(len(slides) - 1, new_index))
        slides.insert(new, slides.pop(index))
        return new

    def set_order(self, order: list[int]) -> None:
        slides = self.data["slides"]
        if sorted(order) != list(range(len(slides))):
            raise ProjectError("El nuevo orden debe contener cada diapositiva exactamente una vez.")
        self.data["slides"] = [slides[i] for i in order]


class ProjectManager:
    """Crea, lista, abre y recupera proyectos."""

    def __init__(self, projects_dir: Path | None = None):
        self.root = Path(projects_dir) if projects_dir else get_settings().projects_path
        self.root.mkdir(parents=True, exist_ok=True)

    def list_projects(self) -> list[dict[str, str]]:
        out = []
        for d in sorted(self.root.iterdir()) if self.root.exists() else []:
            mf = d / MANIFEST
            if d.is_dir() and (mf.exists() or (d / (MANIFEST + ".bak")).exists()):
                try:
                    data = json.loads(mf.read_text(encoding="utf-8"))
                    out.append({"id": d.name, "name": data.get("name", d.name),
                                "updated_at": data.get("updated_at", "")})
                except (OSError, json.JSONDecodeError):
                    out.append({"id": d.name, "name": f"{d.name} (manifiesto dañado: se intentará recuperar)",
                                "updated_at": ""})
        out.sort(key=lambda p: p["updated_at"], reverse=True)
        return out

    def create(self, name: str) -> Project:
        name = (name or "").strip()
        if not name:
            raise ProjectError("Escribe un nombre para el proyecto.")
        base = slugify(name)
        pid = base
        n = 2
        while (self.root / pid).exists():
            pid = f"{base}-{n}"
            n += 1
        data = default_project(name)
        data["id"] = pid
        proj = Project(self.root / pid, data)
        proj.dir.mkdir(parents=True)
        proj.ensure_dirs()
        proj.save()
        log.info("Proyecto creado: %s", proj.dir)
        return proj

    def open(self, project_id: str) -> Project:
        d = safe_join(self.root, project_id)
        mf = d / MANIFEST
        bak = d / (MANIFEST + ".bak")
        data = None
        errors = []
        for candidate in (mf, bak):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if candidate is bak:
                    log.warning("Proyecto %s recuperado desde la copia de seguridad.", project_id)
                break
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{candidate.name}: {exc}")
        if data is None:
            raise ProjectError(f"No se pudo abrir el proyecto {project_id!r}. " + "; ".join(errors))
        data = migrate(data)
        data["id"] = d.name
        proj = Project(d, data)
        proj.ensure_dirs()
        return proj

    def duplicate_settings_into(self, source: Project, target: Project) -> None:
        """Copia diseño, voz, audio, tiempos y exportación de un proyecto a otro."""
        for key in ("design", "voice", "audio", "timing", "export"):
            target.data[key] = deepcopy(source.data[key])
        target.save()
