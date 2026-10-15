"""Creación y sincronización de subtítulos (ASS para quemar en el vídeo, SRT independiente).

Sincronización: cada frase tiene inicio y fin REALES medidos en su audio. Si una frase
no cabe en pantalla (líneas × caracteres), se divide en bloques por pausas naturales
(comas, punto y coma, dos puntos) y, si hace falta, por palabras; el tiempo de la
frase se reparte entre sus bloques en proporción al número de caracteres. No se
simula sincronización palabra a palabra.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from image_processor import compute_layout, hex_to_rgb
from script_manager import format_srt_time
from timeline import Timeline

MIN_CUE = 0.6   # segundos mínimos por bloque dentro de una frase


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]
    item_index: int

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Reparte el texto en líneas de como máximo ``max_chars`` caracteres (equilibradas)."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    # Equilibrar dos líneas para evitar una línea muy corta al final
    if len(lines) == 2:
        all_words = text.split()
        best = lines
        best_diff = abs(len(lines[0]) - len(lines[1]))
        for k in range(1, len(all_words)):
            a, b = " ".join(all_words[:k]), " ".join(all_words[k:])
            if len(a) <= max_chars and len(b) <= max_chars and abs(len(a) - len(b)) < best_diff:
                best, best_diff = [a, b], abs(len(a) - len(b))
        lines = best
    return lines


def split_phrases(sentence: str) -> list[str]:
    parts = re.split(r"(?<=[,;:—])\s+", sentence.strip())
    return [p for p in parts if p]


def _balanced_split(words: list[str], max_chars: int, max_lines: int) -> list[str]:
    """Reparte palabras en el menor número de bloques que caben, con longitudes equilibradas."""
    text_len = len(" ".join(words))
    for n in range(2, len(words) + 1):
        target = text_len / n
        blocks: list[str] = []
        cur: list[str] = []
        for i, w in enumerate(words):
            remaining_words = len(words) - i
            remaining_blocks = n - len(blocks)
            cand = " ".join(cur + [w])
            if cur and (len(cand) > target * 1.15 or remaining_words < remaining_blocks) and \
                    len(blocks) < n - 1:
                blocks.append(" ".join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            blocks.append(" ".join(cur))
        if all(len(wrap_lines(b, max_chars)) <= max_lines for b in blocks):
            return blocks
    return words


def chunk_sentence(sentence: str, max_chars: int, max_lines: int) -> list[list[str]]:
    """Divide una frase en bloques de pantalla (cada bloque = lista de líneas, como mucho ``max_lines``)."""
    fits = lambda t: len(wrap_lines(t, max_chars)) <= max_lines
    blocks: list[str] = []
    cur = ""
    for phrase in split_phrases(sentence):
        cand = f"{cur} {phrase}".strip()
        if fits(cand):
            cur = cand
            continue
        if cur:
            blocks.append(cur)
            cur = ""
        if fits(phrase):
            cur = phrase
        else:
            blocks.extend(_balanced_split(phrase.split(), max_chars, max_lines))
    if cur:
        blocks.append(cur)
    return [wrap_lines(b, max_chars) for b in blocks]


def build_cues(timeline: Timeline, max_chars: int = 26, max_lines: int = 2) -> list[Cue]:
    cues: list[Cue] = []
    for it in timeline.items:
        if it.narration_start is None:
            continue
        for seg in it.segments:
            s0 = it.narration_start + float(seg["start"])
            s1 = it.narration_start + float(seg["end"])
            blocks = chunk_sentence(seg["text"], max_chars, max_lines)
            if not blocks:
                continue
            # Unir bloques que quedarían demasiado cortos
            lengths = [sum(len(l) for l in b) for b in blocks]
            total_chars = sum(lengths) or 1
            dur = s1 - s0
            while len(blocks) > 1 and min(dur * L / total_chars for L in lengths) < MIN_CUE:
                i = min(range(len(lengths)), key=lambda j: lengths[j])
                merged_ok = False
                for j in (i - 1, i + 1):          # unir con un vecino sólo si sigue cabiendo
                    if 0 <= j < len(blocks):
                        a, b = sorted((i, j))
                        merged = wrap_lines(" ".join(blocks[a] + blocks[b]), max_chars)
                        if len(merged) <= max_lines:
                            blocks[a:b + 1] = [merged]
                            merged_ok = True
                            break
                if not merged_ok:
                    break
                lengths = [sum(len(l) for l in bl) for bl in blocks]
            t = s0
            for b, L in zip(blocks, lengths):
                end = t + dur * L / total_chars
                cues.append(Cue(round(t, 3), round(end, 3), b, it.index))
                t = end
            cues[-1].end = round(s1, 3)
    return cues


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------

def _ass_color(hex_color: str, alpha: float = 0.0) -> str:
    r, g, b = hex_to_rgb(hex_color)
    a = int(round(min(1.0, max(0.0, alpha)) * 255))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    text = text.replace("\\", " ").replace("{", "(").replace("}", ")")
    return re.sub(r"\s+", " ", text).strip()


def write_ass(cues: list[Cue], path: Path, design: dict[str, Any], width: int, height: int,
              font_family: str) -> Path:
    k = width / 1080.0
    lay = compute_layout(design, width, height)
    size = int(round(design.get("subtitle_size", 62) * k))
    box = bool(design.get("subtitle_box", True))
    primary = _ass_color(design.get("subtitle_color", "#ffffff"))
    if box:
        # libass dibuja la caja opaca (BorderStyle 3) con OutlineColour
        opacity = float(design.get("subtitle_box_opacity", 0.55))
        outline_col = _ass_color("#000000" if hex_to_rgb(design.get("subtitle_color", "#fff"))[0] > 128 else "#ffffff",
                                 1.0 - opacity)
        border_style, outline, shadow = 3, max(4, int(14 * k)), 0
    else:
        outline_col = _ass_color(design.get("subtitle_outline_color", "#000000"))
        border_style, outline, shadow = 1, max(2, int(5 * k)), max(1, int(2 * k))
    bold = -1 if design.get("font_weight_bold", True) else 0
    margin = int(design.get("subtitle_margin_x", 70) * k)
    header = [
        "[Script Info]",
        "; Generado por ShortsFactory",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{font_family},{size},{primary},{primary},{outline_col},&H80000000,{bold},0,0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},5,{margin},{margin},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    cx, cy = width // 2, lay.subtitle_center_y
    events = []
    for c in cues:
        text = r"\N".join(ass_escape(l) for l in c.lines)
        events.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Sub,,0,0,0,,{{\\an5\\pos({cx},{cy})}}{text}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(header + events) + "\n", encoding="utf-8-sig")
    return path


def write_srt(cues: list[Cue], path: Path) -> Path:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{format_srt_time(c.start)} --> {format_srt_time(c.end)}")
        out.extend(c.lines)
        out.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8-sig")
    return path


def sample_cues(text: str, design: dict[str, Any], duration: float = 3.0) -> list[Cue]:
    blocks = chunk_sentence(text, int(design.get("subtitle_max_chars", 26)), int(design.get("subtitle_max_lines", 2)))
    return [Cue(0.0, duration, blocks[0] if blocks else [text], 0)]
