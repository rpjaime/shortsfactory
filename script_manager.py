"""Edición, validación, importación y exportación de guiones.

Incluye el cliente opcional de LM Studio (API local compatible con OpenAI).
Nunca se contacta con servicios externos: la URL debe apuntar a un servidor local.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_manager import SLIDE_ROLES, PathSecurityError, Project, safe_join

log = logging.getLogger(__name__)

SCRIPT_SCHEMA_VERSION = 1
MAX_WORDS_PER_SLIDE = 55        # ~20 s de voz: a partir de aquí se avisa
MAX_IMPORT_BYTES = 2_000_000


class ScriptError(Exception):
    pass


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_markdown(self) -> str:
        if not self.errors and not self.warnings:
            return "✅ El guion es válido."
        lines = []
        if self.errors:
            lines.append("**Errores (impiden generar):**")
            lines += [f"- ❌ {e}" for e in self.errors]
        if self.warnings:
            lines.append("**Avisos:**")
            lines += [f"- ⚠️ {w}" for w in self.warnings]
        return "\n".join(lines)


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def validate_narration_text(text: str, label: str, result: ValidationResult) -> None:
    words = len(text.split())
    if words > MAX_WORDS_PER_SLIDE:
        result.warnings.append(f"{label}: {words} palabras; puede quedar demasiado larga para un Short.")
    if re.search(r"https?://|www\.", text):
        result.warnings.append(f"{label}: contiene una URL; la voz la leerá letra a letra.")
    if re.search(r"[<>{}\[\]|\\^~]", text):
        result.warnings.append(f"{label}: contiene símbolos que la voz puede pronunciar mal.")
    if text and not re.search(r"[.!?…]$", text):
        result.warnings.append(f"{label}: no termina en signo de puntuación; la entonación puede quedar abierta.")


def validate_project_script(project: Project) -> ValidationResult:
    res = ValidationResult()
    slides = project.enabled_slides()
    if not slides:
        res.errors.append("No hay ninguna diapositiva activa.")
    for i, s in enumerate(slides, 1):
        label = f"Diapositiva {i} ({s['image']})"
        text = normalize_text(s.get("narration", ""))
        if s.get("role") == "silent":
            if text:
                res.warnings.append(f"{label}: marcada como «sin voz» pero tiene texto; se ignorará.")
            continue
        if not text:
            res.errors.append(f"{label}: falta la narración (o márcala con el rol «silent»).")
            continue
        validate_narration_text(text, label, res)
    outro = project.data.get("outro", {})
    if outro.get("enabled") and normalize_text(outro.get("narration", "")):
        validate_narration_text(normalize_text(outro["narration"]), "Cierre", res)
    return res


def validate_script_json(data: Any, available_images: list[str] | None = None) -> ValidationResult:
    """Valida un guion importado (formato del ejemplo de la especificación)."""
    res = ValidationResult()
    if not isinstance(data, dict):
        res.errors.append("El guion debe ser un objeto JSON.")
        return res
    ver = data.get("schema_version")
    if ver is None:
        res.errors.append("Falta «schema_version».")
    elif not isinstance(ver, int) or ver < 1:
        res.errors.append("«schema_version» debe ser un entero ≥ 1.")
    elif ver > SCRIPT_SCHEMA_VERSION:
        res.errors.append(f"«schema_version» {ver} no soportado (máximo {SCRIPT_SCHEMA_VERSION}).")
    for key in ("title", "language", "voice"):
        if key in data and not isinstance(data[key], str):
            res.errors.append(f"«{key}» debe ser texto.")
    if "target_duration_seconds" in data:
        v = data["target_duration_seconds"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 5 <= v <= 180:
            res.errors.append("«target_duration_seconds» debe ser un número entre 5 y 180.")
    slides = data.get("slides")
    if not isinstance(slides, list) or not slides:
        res.errors.append("«slides» debe ser una lista con al menos una diapositiva.")
        return res
    seen = set()
    for i, s in enumerate(slides, 1):
        if not isinstance(s, dict):
            res.errors.append(f"Diapositiva {i}: debe ser un objeto.")
            continue
        img = s.get("image")
        if not isinstance(img, str) or not img.strip():
            res.errors.append(f"Diapositiva {i}: falta «image».")
        else:
            if "/" in img or "\\" in img or ".." in img or re.match(r"^[a-zA-Z]:", img):
                res.errors.append(f"Diapositiva {i}: «image» debe ser sólo un nombre de archivo, sin rutas ({img!r}).")
            elif available_images is not None and img not in available_images:
                res.errors.append(f"Diapositiva {i}: la imagen {img!r} no está en la carpeta del proyecto.")
            if img in seen:
                res.warnings.append(f"Diapositiva {i}: la imagen {img!r} aparece repetida.")
            seen.add(img)
        role = s.get("role", "content")
        if role not in SLIDE_ROLES:
            res.errors.append(f"Diapositiva {i}: rol {role!r} no válido ({', '.join(SLIDE_ROLES)}).")
        nar = s.get("narration", "")
        if not isinstance(nar, str):
            res.errors.append(f"Diapositiva {i}: «narration» debe ser texto.")
        for opt in ("headline", "notes"):
            if opt in s and not isinstance(s[opt], str):
                res.errors.append(f"Diapositiva {i}: «{opt}» debe ser texto.")
    outro = data.get("outro")
    if outro is not None and (not isinstance(outro, dict) or not isinstance(outro.get("narration", ""), str)):
        res.errors.append("«outro» debe ser un objeto con «narration» de texto.")
    return res


# ---------------------------------------------------------------------------
# Importar / exportar
# ---------------------------------------------------------------------------

def _read_limited(path: str | Path) -> str:
    p = Path(path)
    if p.stat().st_size > MAX_IMPORT_BYTES:
        raise ScriptError("El archivo es demasiado grande para ser un guion.")
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ScriptError("No se pudo leer el archivo como texto.")


def import_script_json(project: Project, path: str | Path, available_images: list[str]) -> ValidationResult:
    try:
        data = json.loads(_read_limited(path))
    except json.JSONDecodeError as exc:
        raise ScriptError(f"JSON no válido (línea {exc.lineno}, columna {exc.colno}): {exc.msg}") from exc
    res = validate_script_json(data, available_images)
    if not res.ok:
        return res
    src = project.source_dir
    by_image = {s["image"]: s for s in project.data["slides"]}
    new_order = []
    for item in data["slides"]:
        if src is not None:
            try:
                safe_join(src, item["image"])
            except PathSecurityError as exc:
                res.errors.append(str(exc))
                return res
        slide = by_image.pop(item["image"], None)
        if slide is None:
            continue
        slide["narration"] = normalize_text(item.get("narration", ""))
        slide["role"] = item.get("role", slide.get("role", "content"))
        if "headline" in item:
            slide["headline"] = item["headline"]
        if "notes" in item:
            slide["notes"] = item["notes"]
        slide["enabled"] = True
        new_order.append(slide)
    for leftover in by_image.values():   # imágenes no citadas: se conservan al final, desactivadas
        leftover["enabled"] = False
        new_order.append(leftover)
        res.warnings.append(f"{leftover['image']} no aparece en el guion: se ha desactivado.")
    project.data["slides"] = new_order
    if isinstance(data.get("title"), str):
        project.data["title"] = data["title"]
    if isinstance(data.get("language"), str):
        project.data["language"] = data["language"]
    if isinstance(data.get("target_duration_seconds"), (int, float)):
        project.data["timing"]["target_duration"] = float(data["target_duration_seconds"])
    if isinstance(data.get("voice"), str) and data["voice"] not in ("", "default"):
        project.data["voice"]["voice_id"] = data["voice"]
    outro = data.get("outro")
    if isinstance(outro, dict):
        project.data["outro"]["narration"] = normalize_text(outro.get("narration", ""))
        project.data["outro"]["enabled"] = bool(project.data["outro"]["narration"]) or outro.get("enabled", False)
        if isinstance(outro.get("text"), str):
            project.data["outro"]["text"] = outro["text"]
    return res


def export_script_json(project: Project) -> dict[str, Any]:
    return {
        "schema_version": SCRIPT_SCHEMA_VERSION,
        "title": project.data.get("title", project.name),
        "language": project.data.get("language", "es-ES"),
        "voice": project.data["voice"].get("voice_id", "default"),
        "target_duration_seconds": project.data["timing"].get("target_duration", 40),
        "slides": [
            {k: v for k, v in {
                "image": s["image"], "role": s.get("role", "content"), "headline": s.get("headline", ""),
                "narration": s.get("narration", ""), "notes": s.get("notes", ""),
            }.items() if v != "" or k in ("image", "narration")}
            for s in project.enabled_slides()
        ],
        "outro": {"narration": project.data["outro"].get("narration", ""),
                  "text": project.data["outro"].get("text", "")},
    }


SRT_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(text: str) -> list[dict[str, Any]]:
    cues = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        idx = 1 if "-->" in lines[1] else 0
        if "-->" not in lines[idx]:
            raise ScriptError(f"Bloque SRT sin marca de tiempo: {lines[0]!r}")
        a, b2 = lines[idx].split("-->")
        ma, mb = SRT_TIME.search(a), SRT_TIME.search(b2)
        if not ma or not mb:
            raise ScriptError(f"Marca de tiempo SRT no válida: {lines[idx]!r}")
        to_s = lambda m: int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4].ljust(3, "0")) / 1000
        cues.append({"start": to_s(ma), "end": to_s(mb), "text": " ".join(lines[idx + 1:]).strip()})
    return cues


def import_script_srt(project: Project, path: str | Path) -> ValidationResult:
    """Asigna cada bloque SRT a una diapositiva activa, en orden; si sobra uno, va al cierre.

    Las marcas de tiempo del SRT no se usan para sincronizar: la sincronización
    siempre se calcula a partir del audio real generado.
    """
    res = ValidationResult()
    cues = parse_srt(_read_limited(path))
    slides = project.enabled_slides()
    if not cues:
        res.errors.append("El SRT no contiene ningún bloque.")
        return res
    for s, c in zip(slides, cues):
        s["narration"] = normalize_text(c["text"])
    if len(cues) > len(slides):
        rest = " ".join(c["text"] for c in cues[len(slides):])
        project.data["outro"]["narration"] = normalize_text(rest)
        project.data["outro"]["enabled"] = True
        res.warnings.append(f"{len(cues) - len(slides)} bloque(s) sobrante(s) se han asignado al cierre.")
    elif len(cues) < len(slides):
        res.warnings.append(f"El SRT tiene {len(cues)} bloques para {len(slides)} diapositivas; revisa las que faltan.")
    res.warnings.append("Las marcas de tiempo del SRT se ignoran: los tiempos se calculan con el audio real.")
    return res


def format_srt_time(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# LM Studio (opcional, sólo local)
# ---------------------------------------------------------------------------

class LLMError(Exception):
    pass


def _check_local_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError(f"URL de LM Studio no válida: {url!r}")
    host = parsed.hostname.lower()
    local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or \
        re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host)
    if not local:
        raise LLMError("Por privacidad, sólo se permite un servidor LM Studio local o de tu red privada "
                       f"(localhost, 127.0.0.1, 192.168.x.x…). Recibido: {host}")
    return url


def _http_json(url: str, payload: dict | None, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    # Sin proxies: la petición nunca debe salir del equipo/red local
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"LM Studio respondió con error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"No se puede conectar con LM Studio en {url}. ¿Está abierto con el servidor "
                       f"local iniciado? ({exc.reason})") from exc
    except TimeoutError as exc:
        raise LLMError(f"LM Studio no respondió en {timeout:.0f} s. Aumenta el tiempo de espera o usa un "
                       "modelo más pequeño.") from exc
    except json.JSONDecodeError as exc:
        raise LLMError("LM Studio devolvió una respuesta que no es JSON.") from exc


VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "recurso": {"type": "string"},
        "texto_visible": {"type": "string"},
        "descripcion": {"type": "string"},
        "es_portada": {"type": "boolean"},
        "legible": {"type": "boolean"},
    },
    "required": ["recurso", "texto_visible", "descripcion", "es_portada", "legible"],
}
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "slides": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "narration": {"type": "string"},
                           "transition": {"type": "string"}, "needs_more_info": {"type": "boolean"}},
            "required": ["index", "narration"]}},
        "outro": {"type": "string"},
    },
    "required": ["hook", "slides", "outro"],
}


def structured_completion(url: str, payload: dict, timeout: float, schema: dict, name: str) -> tuple[str, str | None]:
    """Pide salida JSON forzada por esquema (LM Studio ≥ 0.3 la admite). Si el servidor no la acepta,
    repite la petición sin ella."""
    with_schema = dict(payload, response_format={"type": "json_schema",
                                                 "json_schema": {"name": name, "strict": True, "schema": schema}})
    try:
        return chat_completion(url, with_schema, timeout)
    except LLMError as exc:
        if "error 400" in str(exc) or "response_format" in str(exc):
            log.info("El servidor no admite response_format; se repite sin esquema.")
            return chat_completion(url, payload, timeout)
        raise


def chat_completion(url: str, payload: dict, timeout: float, on_token=None) -> tuple[str, str | None]:
    """Llama a /chat/completions en modo streaming. Devuelve (contenido, finish_reason).

    El tiempo de espera se aplica a cada fragmento recibido: una respuesta larga no falla
    mientras el modelo siga enviando texto; sólo falla si LM Studio deja de responder.
    """
    body = dict(payload, stream=True)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    content: list[str] = []
    reasoning: list[str] = []
    finish = None
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "event-stream" not in ctype:      # servidor sin streaming: respuesta normal
                data = json.loads(resp.read().decode("utf-8"))
                msg = data["choices"][0]["message"]
                return (msg.get("content") or msg.get("reasoning_content") or ""), data["choices"][0].get("finish_reason")
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                    ch = obj["choices"][0]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    if on_token:
                        on_token(len(content))
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                finish = ch.get("finish_reason") or finish
    except urllib.error.HTTPError as exc:
        raise LLMError(f"LM Studio respondió con error {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason):
            raise LLMError(f"LM Studio dejó de responder durante {timeout:.0f} s.") from exc
        raise LLMError(f"No se puede conectar con LM Studio en {url}. ¿Está abierto con el servidor local "
                       f"iniciado? ({exc.reason})") from exc
    except (TimeoutError, OSError) as exc:
        raise LLMError(f"LM Studio dejó de responder durante {timeout:.0f} s ({exc}).") from exc
    text = "".join(content)
    if "{" not in text and reasoning:
        text = "".join(reasoning)
    return text, finish


def lmstudio_list_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """Modelos de chat disponibles; los que LM Studio tiene CARGADOS en memoria van primero."""
    url = _check_local_url(base_url)
    data = _http_json(f"{url}/models", None, timeout)
    ids = [m.get("id", "") for m in data.get("data", []) if m.get("id") and "embed" not in m.get("id", "").lower()]
    loaded = lmstudio_loaded_models(url, timeout)
    return [m for m in ids if m in loaded] + [m for m in ids if m not in loaded]


def lmstudio_loaded_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """Modelos cargados según la API nativa de LM Studio (/api/v0/models). Vacío si no está disponible."""
    url = _check_local_url(base_url)
    root = url[:-3] if url.endswith("/v1") else url
    try:
        data = _http_json(f"{root}/api/v0/models", None, timeout)
    except LLMError:
        return []
    return [m.get("id", "") for m in data.get("data", [])
            if m.get("state") == "loaded" and m.get("type") in (None, "llm", "vlm")]


SYSTEM_PROMPT = """Eres guionista de vídeos cortos verticales (YouTube Shorts) en español de España.
Escribes narraciones breves, claras y naturales para ser leídas por una voz sintética.

Reglas obligatorias:
- Usa EXCLUSIVAMENTE la información que aporta el usuario para cada diapositiva. No inventes
  características, funciones, estadísticas, número de estrellas, licencias, precios, autores ni fechas.
- Si una diapositiva no tiene información suficiente, escribe una frase prudente y neutra que no afirme
  nada concreto sobre el recurso, y márcala con "needs_more_info": true.
- Frases cortas (máximo unas 25 palabras por frase), sin emojis, sin URLs, sin hashtags, sin listas.
- Escribe los números y siglas de forma que una voz los lea bien.
- Español de España (tú, vosotros), tono cercano y directo.
- Responde SOLO con un objeto JSON válido, sin texto adicional, con esta forma:
{"hook": "...", "slides": [{"index": 1, "narration": "...", "transition": "", "needs_more_info": false}],
 "outro": "..."}
"""


def build_llm_messages(project: Project, topic: str = "", extra: str = "") -> list[dict[str, str]]:
    slides = project.enabled_slides()
    parts = [f"Título del vídeo: {project.data.get('title') or project.name}"]
    if topic.strip():
        parts.append(f"Tema general: {topic.strip()}")
    if extra.strip():
        parts.append(f"Indicaciones adicionales: {extra.strip()}")
    target = project.data["timing"].get("target_duration", 40)
    parts.append(f"Duración objetivo aproximada: {target:.0f} segundos en total (unas {int(target * 2.4)} palabras).")
    tk = (project.data.get("tiktok") or {}).get("description", "").strip()
    if tk:
        parts.append(f"Texto original de la publicación de TikTok (contexto; no lo copies literalmente): {tk[:600]}")
    parts.append("La diapositiva 1 es la portada: su narración debe ser el gancho inicial.")
    parts.append("Diapositivas (información aportada por el usuario):")
    for i, s in enumerate(slides, 1):
        info = (s.get("notes") or "").strip() or "(sin información aportada)"
        head = (s.get("headline") or "").strip()
        parts.append(f"{i}. Imagen «{s['image']}»" + (f" — titular: {head}" if head else "") + f"\n   Información: {info}")
    parts.append("Incluye un cierre con llamada a la acción (seguir el canal o guardar el vídeo).")
    # «/no_think» desactiva el modo de razonamiento de Qwen3 (otros modelos lo ignoran): respuesta más rápida
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts) + "\n\n/no_think"}]


def extract_json_object(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # modelos de razonamiento
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("La respuesta del modelo no contiene un objeto JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"El modelo devolvió JSON no válido: {exc.msg}") from exc


def slides_missing_info(project: Project) -> list[int]:
    return [i for i, s in enumerate(project.enabled_slides(), 1) if not (s.get("notes") or "").strip()]


def generate_draft(project: Project, base_url: str, model: str, timeout: float,
                   topic: str = "", extra: str = "") -> dict[str, Any]:
    """Pide un borrador a LM Studio. Devuelve {'slides': [...], 'outro': str, 'warnings': [...]}.

    No modifica el proyecto: el resultado se muestra para revisión y edición.
    """
    url = _check_local_url(base_url)
    if not model.strip():
        models = lmstudio_list_models(url, timeout=min(10.0, timeout))
        if not models:
            raise LLMError("LM Studio no tiene ningún modelo cargado.")
        model = models[0]
    payload = {
        "model": model,
        "messages": build_llm_messages(project, topic, extra),
        "temperature": 0.4,
        "max_tokens": 4096,
    }
    content, finish = structured_completion(f"{url}/chat/completions", payload, timeout, SCRIPT_SCHEMA, "guion")
    if finish == "length" and "}" not in content:
        raise LLMError("El modelo agotó el límite de tokens antes de terminar (¿modo de razonamiento activo?). "
                       "Prueba otra vez o usa un modelo sin razonamiento.")
    obj = extract_json_object(content)
    return parse_draft(obj, len(project.enabled_slides()))


def parse_draft(obj: dict, n_slides: int) -> dict[str, Any]:
    warnings = []
    narrations = [""] * n_slides
    flags = [False] * n_slides
    items = obj.get("slides") if isinstance(obj.get("slides"), list) else []
    for pos, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        idx = it.get("index")
        idx = int(idx) - 1 if isinstance(idx, (int, float, str)) and str(idx).isdigit() else pos
        if not 0 <= idx < n_slides:
            continue
        text = normalize_text(str(it.get("narration", "")))
        trans = normalize_text(str(it.get("transition", "") or ""))
        if trans:
            text = f"{text} {trans}".strip()
        narrations[idx] = text
        flags[idx] = bool(it.get("needs_more_info"))
    hook = normalize_text(str(obj.get("hook", "") or ""))
    if hook and n_slides:
        if hook not in narrations[0]:
            narrations[0] = f"{hook} {narrations[0]}".strip()
    for i, f in enumerate(flags):
        if f:
            warnings.append(f"Diapositiva {i + 1}: el modelo indica que falta información; revisa el texto.")
    for i, t in enumerate(narrations):
        if not t:
            warnings.append(f"Diapositiva {i + 1}: el modelo no generó narración.")
    return {"slides": narrations, "outro": normalize_text(str(obj.get("outro", "") or "")), "warnings": warnings}


# ---------------------------------------------------------------------------
# Lectura de imágenes con un modelo de visión (LM Studio)
# ---------------------------------------------------------------------------

VISION_PROMPT = """Analiza esta diapositiva de un carrusel para redes sociales (normalmente capturas de pantalla
de repositorios de GitHub, herramientas o webs). Responde SOLO con un objeto JSON válido:
{"recurso": "nombre exacto de la herramienta o repositorio si aparece ESCRITO en la imagen, o cadena vacía",
 "texto_visible": "transcripción literal del texto relevante que se lee (títulos, descripción, etiquetas); máximo 600 caracteres",
 "descripcion": "una o dos frases en español de España sobre lo que muestra la imagen",
 "es_portada": true/false,
 "legible": true/false}
Reglas: no inventes nada que no se vea en la imagen. No supongas estrellas, licencias, precios ni funciones que no
estén escritas. Si no se lee bien, pon "legible": false. /no_think"""

AUTO_NOTES_PREFIX = "[Leído de la imagen]"


def lmstudio_model_info(base_url: str, timeout: float = 5.0) -> list[dict[str, str]]:
    """[{'id', 'type' (llm|vlm|embeddings|?), 'state' (loaded|not-loaded|?)}] según la API nativa de LM Studio."""
    url = _check_local_url(base_url)
    root = url[:-3] if url.endswith("/v1") else url
    try:
        data = _http_json(f"{root}/api/v0/models", None, timeout)
        return [{"id": m.get("id", ""), "type": m.get("type", "?"), "state": m.get("state", "?")}
                for m in data.get("data", []) if m.get("id")]
    except LLMError:
        return [{"id": m, "type": "?", "state": "?"} for m in lmstudio_list_models(url, timeout)]


def choose_models(base_url: str, text_model: str, vision_model: str, timeout: float = 10.0,
                  need_vision: bool = True) -> tuple[str, str | None, list[str]]:
    """Devuelve (modelo_texto, modelo_visión|None, avisos). Prefiere modelos ya cargados."""
    info = lmstudio_model_info(base_url, timeout)
    notes: list[str] = []
    chat = [m for m in info if m["type"] not in ("embeddings",) and "embed" not in m["id"].lower()]
    if not chat:
        raise LLMError("LM Studio no tiene ningún modelo de chat disponible.")
    by_id = {m["id"]: m for m in chat}
    rank = lambda m: (m["state"] != "loaded", m["type"] != "vlm")
    vlm = None
    if need_vision:
        if vision_model.strip():
            vlm = vision_model.strip()
            if vlm in by_id and by_id[vlm]["type"] not in ("vlm", "?"):
                raise LLMError(f"El modelo «{vlm}» no admite imágenes (tipo {by_id[vlm]['type']}). Elige un modelo "
                               "con visión en Ajustes o carga uno en LM Studio (p. ej. Gemma o Qwen-VL).")
        else:
            cands = sorted([m for m in chat if m["type"] == "vlm"], key=rank)
            if cands:
                vlm = cands[0]["id"]
                if cands[0]["state"] != "loaded":
                    notes.append(f"LM Studio cargará el modelo con visión «{vlm}» (puede tardar).")
            elif any(m["type"] == "?" for m in chat):
                vlm = text_model.strip() or sorted(chat, key=rank)[0]["id"]
                notes.append("No se pudo saber qué modelos tienen visión; se intentará con el modelo actual.")
            else:
                raise LLMError("Ningún modelo de LM Studio admite imágenes. Descarga y carga uno con visión "
                               "(por ejemplo Gemma 3/4 o Qwen2.5-VL) o desmarca «Leer las imágenes».")
    if text_model.strip():
        txt = text_model.strip()
    elif vlm:
        txt = vlm          # usar el mismo modelo evita que LM Studio cargue dos modelos grandes a la vez
    else:
        loaded = sorted(chat, key=rank)
        txt = loaded[0]["id"]
    return txt, vlm, notes


def encode_image_for_llm(path: str | Path, max_side: int = 1024) -> str:
    from PIL import Image, ImageOps
    import base64
    import io
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def read_slide_image(base_url: str, model: str, timeout: float, image_path: str | Path) -> dict[str, Any]:
    url = _check_local_url(base_url)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": encode_image_for_llm(image_path)}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    content, finish = structured_completion(f"{url}/chat/completions", payload, timeout, VISION_SCHEMA, "lectura")
    try:
        obj = extract_json_object(content)
    except LLMError:
        log.warning("Respuesta no JSON al leer %s (fin=%s): %r", Path(image_path).name, finish, content[:300])
        if finish == "length" and not content.strip():
            raise LLMError("El modelo gastó todo el espacio de respuesta razonando sin contestar.") from None
        plain = normalize_text(re.sub(r"<think>.*?</think>", "", content, flags=re.S))
        if len(plain) < 20:
            raise
        obj = {"descripcion": plain[:400], "texto_visible": "", "recurso": "", "legible": True}
    return {
        "recurso": normalize_text(str(obj.get("recurso", "") or ""))[:120],
        "texto_visible": normalize_text(str(obj.get("texto_visible", "") or ""))[:800],
        "descripcion": normalize_text(str(obj.get("descripcion", "") or ""))[:400],
        "es_portada": bool(obj.get("es_portada", False)),
        "legible": bool(obj.get("legible", True)),
    }


def reading_to_notes(r: dict[str, Any]) -> str:
    parts = [AUTO_NOTES_PREFIX]
    if r.get("recurso"):
        parts.append(f"Recurso: {r['recurso']}.")
    if r.get("texto_visible"):
        parts.append(f"Texto visible: {r['texto_visible']}")
    if r.get("descripcion"):
        parts.append(f"Descripción: {r['descripcion']}")
    if not r.get("legible", True):
        parts.append("(La imagen no se lee bien: narración prudente.)")
    return " ".join(parts)


def image_fingerprint(path: str | Path) -> str:
    import hashlib
    p = Path(path)
    st = p.stat()
    return hashlib.sha256(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:16]


def auto_script(project: Project, base_url: str, text_model: str, vision_model: str, timeout: float,
                read_images: bool = True, overwrite: bool = False, topic: str = "", extra: str = "",
                progress=None) -> dict[str, Any]:
    """Lee las imágenes (opcional), genera el guion y lo escribe en el proyecto.

    Sólo rellena las narraciones vacías salvo que ``overwrite`` sea True. Las notas escritas a mano por el
    usuario nunca se sobrescriben con la lectura automática.
    """
    say = progress or (lambda frac, msg: None)
    slides = project.enabled_slides()
    if not slides:
        raise LLMError("No hay diapositivas activas.")
    txt_model, vlm, warnings = choose_models(base_url, text_model, vision_model, min(15.0, timeout), read_images)
    read = 0
    reused = 0
    failed: list[int] = []
    last_error = ""
    if read_images:
        for i, s in enumerate(slides, 1):
            notes = (s.get("notes") or "").strip()
            if notes and not notes.startswith(AUTO_NOTES_PREFIX):
                continue                         # notas del usuario: mandan sobre la lectura automática
            path = project.image_path(s["image"])
            fp = image_fingerprint(path)
            prev = s.get("vision") or {}
            if prev.get("fp") == fp and prev.get("result"):
                result = prev["result"]
                reused += 1
            else:
                say((i - 1) / (len(slides) + 1), f"Leyendo imagen {i}/{len(slides)}: {s['image']}")
                result = None
                for attempt in (1, 2):
                    try:
                        t0 = time.time()
                        result = read_slide_image(base_url, vlm or txt_model, timeout, path)
                        log.info("Imagen %s leída en %.1f s", s["image"], time.time() - t0)
                        break
                    except LLMError as exc:
                        log.warning("Lectura de %s fallida (intento %d): %s", s["image"], attempt, exc)
                        last_error = str(exc)
                if result is None:
                    failed.append(i)
                    warnings.append(f"Diapositiva {i} ({s['image']}): no se pudo leer ({last_error}). Vuelve a "
                                    "pulsar el botón para reintentarla o escribe sus notas a mano.")
                    continue
                s["vision"] = {"fp": fp, "model": vlm, "result": result}
                read += 1
            s["notes"] = reading_to_notes(result)
            project.save()                       # guardar cada lectura: nada se pierde si algo falla
            if not result.get("legible", True):
                warnings.append(f"Diapositiva {i}: la imagen no se lee con claridad; revisa su narración.")
        project.save()
    if failed and len(failed) == len(slides):
        raise LLMError(f"No se pudo leer ninguna imagen: {last_error}")
    say(len(slides) / (len(slides) + 1), "Escribiendo el guion…")
    draft = generate_draft(project, base_url, txt_model, timeout, topic, extra)
    warnings += draft["warnings"]
    written = 0
    for s, text in zip(slides, draft["slides"]):
        if text and (overwrite or not (s.get("narration") or "").strip()):
            s["narration"] = text
            written += 1
    outro = project.data["outro"]
    if draft["outro"] and (overwrite or not (outro.get("narration") or "").strip()):
        outro["narration"] = draft["outro"]
        outro["enabled"] = True
    project.data["llm_notes"] = {"topic": topic, "extra": extra}
    project.save()
    say(1.0, "Guion listo")
    return {"written": written, "total": len(slides), "read": read, "reused": reused, "failed": failed,
            "text_model": txt_model,
            "vision_model": vlm, "warnings": warnings}
