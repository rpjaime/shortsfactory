"""Importación de las imágenes de un carrusel (publicación de fotos) de TikTok a partir de su URL.

Usa gallery-dl (GPL-2.0) como programa aparte (``python -m gallery_dl``), sólo cuando el usuario
pega un enlace y pulsa «Importar». Se descargan únicamente las imágenes (sin vídeo ni música), en
su orden, y el texto de la publicación se guarda para poder usarlo como contexto del guion.

Úsalo con tus propias publicaciones o con contenido que tengas permiso para reutilizar.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from config import IMAGE_EXTENSIONS

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ALLOWED_HOSTS = ("tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com")
TIMEOUT = 180


class TikTokImportError(Exception):
    pass


@dataclass
class TikTokPost:
    folder: Path
    images: list[str]
    post_id: str = ""
    author: str = ""
    description: str = ""
    url: str = ""
    warnings: list[str] = field(default_factory=list)


def normalize_url(url: str) -> str:
    """Valida que sea un enlace de TikTok (https) y lo limpia de parámetros de seguimiento."""
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        raise TikTokImportError("Pega el enlace de la publicación de TikTok.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise TikTokImportError(f"El enlace no es de TikTok ({host or 'sin dominio'}).")
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        if not re.match(r"^/[A-Za-z0-9]+/?$", parsed.path):
            raise TikTokImportError("Enlace corto de TikTok no válido.")
    elif not re.search(r"/(photo|video)/\d+", parsed.path):
        raise TikTokImportError("El enlace debe ser de una publicación concreta: "
                                "https://www.tiktok.com/@usuario/photo/1234567890")
    return urllib.parse.urlunparse(("https", host, parsed.path, "", "", ""))


def build_command(url: str, dest: Path, cookies: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "gallery_dl", "--config-ignore",
           "-o", "photos=true", "-o", "videos=false", "-o", "audio=false", "-o", "covers=false",
           "-o", "subtitles=false", "--write-metadata", "--no-mtime",
           "-D", str(dest), "-f", "{num:>02}.{extension}"]
    if cookies:
        cp = Path(cookies)
        if not cp.is_file():
            raise TikTokImportError(f"No se encuentra el archivo de cookies: {cookies}")
        cmd += ["--cookies", str(cp)]
    return cmd + [url]


ERROR_HINTS = [
    (r"ProxyError|ConnectionError|Max retries|Name or service|getaddrinfo|timed out|SSLError",
     "No hay conexión con TikTok (red, proxy o cortafuegos). Comprueba tu conexión a Internet."),
    (r"Unsupported URL", "gallery-dl no reconoce el enlace. Comprueba que es una publicación de fotos o "
                         "actualiza gallery-dl desde Ajustes (TikTok cambia su web a menudo)."),
    (r"HttpError: '?40[13]|403 Forbidden|captcha|verify",
     "TikTok ha bloqueado la descarga (puede pedir verificación). Espera un rato, prueba con un "
     "archivo de cookies de tu sesión o actualiza gallery-dl."),
    (r"404|not found|status code 10216|10204|private",
     "La publicación no existe, es privada o tiene restricciones. Si es tuya y privada, usa un archivo "
     "de cookies de tu sesión."),
    (r"Failed to extract post|KeyError|rehydration",
     "TikTok ha cambiado su web y gallery-dl no puede leer la publicación. Pulsa «Actualizar gallery-dl» en Ajustes."),
]


def explain_error(output: str) -> str:
    for pattern, hint in ERROR_HINTS:
        if re.search(pattern, output, re.I):
            return hint
    tail = "\n".join(l for l in output.strip().splitlines()[-4:])
    return f"gallery-dl no pudo descargar la publicación.\n{tail}"


def _ensure_supported(path: Path) -> Path:
    """Convierte a PNG cualquier formato que el resto de la aplicación no lea (p. ej. HEIC/AVIF)."""
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return path
    out = path.with_suffix(".png")
    try:
        with Image.open(path) as im:
            im.save(out)
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TikTokImportError(f"Formato de imagen no compatible: {path.name}")
        r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(path),
                            "-frames:v", "1", str(out)], capture_output=True, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out.is_file():
            raise TikTokImportError(f"No se pudo convertir {path.name} a PNG.")
    path.unlink(missing_ok=True)
    return out


def import_post(url: str, project_dir: Path, cookies: str | None = None,
                runner=subprocess.run) -> TikTokPost:
    """Descarga las imágenes de una publicación a ``project_dir/fuente_tiktok/<id>``."""
    clean = normalize_url(url)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(project_dir) / "fuente_tiktok" / f"descarga-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    cmd = build_command(clean, work, cookies)
    log.info("Importando carrusel de TikTok: %s", clean)
    try:
        proc = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=TIMEOUT, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise TikTokImportError(f"La descarga tardó más de {TIMEOUT} s y se ha cancelado.") from exc
    except FileNotFoundError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise TikTokImportError("gallery-dl no está instalado. Ejecuta install.bat.") from exc
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    log.debug("gallery-dl:\n%s", output[-3000:])

    meta_files = sorted(work.glob("*.json"))
    meta: dict = {}
    if meta_files:
        try:
            meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    images = []
    for f in sorted(work.iterdir()):
        if f.suffix.lower() in (".json", ".part", ".mp3", ".m4a", ".mp4"):
            continue
        if f.is_file():
            images.append(_ensure_supported(f))
    for m in meta_files:
        m.unlink(missing_ok=True)

    if not images:
        shutil.rmtree(work, ignore_errors=True)
        if proc.returncode == 0 and meta.get("post_type") == "video":
            raise TikTokImportError("Esa publicación es un vídeo, no un carrusel de fotos.")
        raise TikTokImportError(explain_error(output) if proc.returncode != 0 or output.strip()
                                else "La publicación no contiene imágenes.")

    post_id = str(meta.get("id") or "") or stamp
    final = Path(project_dir) / "fuente_tiktok" / post_id
    if final.exists() and final != work:
        shutil.rmtree(final, ignore_errors=True)
    work.rename(final)
    images = [final / p.name for p in images]
    post = TikTokPost(folder=final, images=[p.name for p in images], post_id=post_id,
                      author=str(meta.get("user") or ""), description=str(meta.get("desc") or "").strip(),
                      url=clean)
    if proc.returncode != 0:
        post.warnings.append("gallery-dl terminó con avisos; revisa que estén todas las imágenes.")
    for p in images:
        try:
            with Image.open(p) as im:
                if min(im.size) < 720:
                    post.warnings.append(f"{p.name} es pequeña ({im.size[0]}×{im.size[1]}): TikTok la sirve comprimida.")
        except Exception:  # noqa: BLE001
            post.warnings.append(f"{p.name} no se puede leer.")
    (final / "publicacion.json").write_text(json.dumps({
        "url": clean, "id": post_id, "autor": post.author, "descripcion": post.description,
        "imagenes": post.images, "importado": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return post


def update_gallery_dl(runner=subprocess.run) -> str:
    """Actualiza gallery-dl en el entorno actual (acción explícita del usuario)."""
    proc = runner([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
                   "gallery-dl"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=300, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise TikTokImportError(f"No se pudo actualizar gallery-dl:\n{(proc.stderr or proc.stdout)[-600:]}")
    ver = runner([sys.executable, "-m", "gallery_dl", "--version"], capture_output=True, text=True,
                 creationflags=NO_WINDOW)
    return (ver.stdout or "").strip()
