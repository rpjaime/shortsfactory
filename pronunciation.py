"""Diccionario de pronunciaciones para la voz sintética.

Los motores de voz (Piper y Kokoro usan espeak-ng) leen mal muchos términos técnicos en
inglés: «GitHub» suena como «jit ub», «IA» como «ya»... Este diccionario sustituye el
término por una grafía que la voz pronuncia bien SÓLO en el texto que se envía al motor
de voz. Los subtítulos conservan siempre el texto original.

El diccionario global se guarda en ``assets/pronunciaciones.json`` y se edita desde la
pestaña *Voz*. Las grafías predeterminadas se han comprobado con la fonetización de
espeak-ng para español (por ejemplo «guítjab» → /ˈɡitxab/).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import get_settings

log = logging.getLogger(__name__)

# (término tal como aparece en el guion, cómo debe leerlo la voz)
DEFAULT_LEXICON: list[tuple[str, str]] = [
    ("GitHub Actions", "guítjab ácshons"),
    ("GitHub", "guítjab"),
    ("GitLab", "guítlab"),
    ("YouTube", "yutub"),
    ("ChatGPT", "chat yi pi ti"),
    ("Copilot", "cópilot"),
    ("JavaScript", "yavascrip"),
    ("TypeScript", "táipscrip"),
    ("Python", "páiton"),
    ("Docker", "dóker"),
    ("README", "rídmi"),
    ("Node.js", "nóud yei es"),
    ("VS Code", "uve ese cóud"),
    ("open source", "ópen sors"),
    ("CLI", "ce ele i"),
    ("LLM", "ele ele eme"),
    ("IA", "i a"),
    ("npm", "ene pe eme"),
    ("frontend", "frónend"),
    ("backend", "bákend"),
    ("framework", "fréimguork"),
]


def lexicon_path() -> Path:
    return get_settings().assets_path / "pronunciaciones.json"


def load_lexicon() -> list[tuple[str, str]]:
    p = lexicon_path()
    if not p.is_file():
        return list(DEFAULT_LEXICON)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        out = [(str(e["termino"]).strip(), str(e["pronunciacion"]).strip()) for e in data.get("entradas", [])
               if str(e.get("termino", "")).strip() and str(e.get("pronunciacion", "")).strip()]
        return out
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        log.warning("No se pudo leer %s (%s); se usa el diccionario predeterminado.", p, exc)
        return list(DEFAULT_LEXICON)


def save_lexicon(entries: list[tuple[str, str]]) -> Path:
    p = lexicon_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = []
    seen = set()
    for term, spoken in entries:
        term, spoken = (term or "").strip(), (spoken or "").strip()
        if not term or not spoken or term.lower() in seen:
            continue
        if len(term) > 80 or len(spoken) > 120:
            raise ValueError(f"Entrada demasiado larga: {term!r}")
        seen.add(term.lower())
        clean.append({"termino": term, "pronunciacion": spoken})
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "entradas": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def apply_lexicon(text: str, entries: list[tuple[str, str]] | None) -> str:
    """Sustituye términos completos (sin distinguir mayúsculas), de los más largos a los más cortos."""
    if not entries or not text:
        return text
    out = text
    for term, spoken in sorted(entries, key=lambda e: len(e[0]), reverse=True):
        pattern = r"(?<![\w])" + re.escape(term) + r"(?![\w])"
        flags = 0 if (term.isupper() and len(term) <= 4) else re.IGNORECASE   # «IA» no debe tocar «ia» de «media»
        out = re.sub(pattern, spoken, out, flags=flags)
    return out
