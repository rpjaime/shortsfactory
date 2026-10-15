import subprocess
from pathlib import Path

import numpy as np
import pytest

import audio_mixer
from conftest import HAS_FFMPEG, needs_ffmpeg
from timeline import build_timeline
from tts_engine import read_wav, write_wav
from video_renderer import OutputSpec, build_final_command, build_pipe_clip_command, build_static_clip_command

BASE = dict(fps=30, transition="fade", transition_duration=0.4, min_slide_duration=1.0, cover_duration=1.0,
            lead_margin=0.15, pause_between=0.3, silent_duration=1.5)


def tone(path: Path, seconds: float, freq=330.0, sr=22050, amp=0.6):
    t = np.arange(int(seconds * sr)) / sr
    write_wav(path, (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr)
    return path


def tl_with_audio(tmp_path, durs):
    entries = []
    for i, d in enumerate(durs):
        p = tone(tmp_path / f"n{i}.wav", d) if d else None
        entries.append({"kind": "slide", "slide_id": str(i), "image": f"{i}.png",
                        "audio_path": str(p) if p else None, "audio_duration": d, "segments": []})
    return build_timeline(entries, **BASE)


def test_mix_command_places_voice_at_timeline(tmp_path):
    tl = tl_with_audio(tmp_path, [1.0, 0.0, 1.5])
    args = audio_mixer.build_mix_command("ffmpeg", tl, {"music_volume": 0.2}, tmp_path / "o.wav",
                                         tmp_path / "m.mp3")
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    graph = args[args.index("-filter_complex") + 1]
    d0 = int(round(tl.items[0].narration_start * 1000))
    d2 = int(round(tl.items[2].narration_start * 1000))
    assert f"adelay={d0}:all=1" in graph and f"adelay={d2}:all=1" in graph
    assert "sidechaincompress" in graph and "alimiter" in graph
    assert args[args.index("-stream_loop") + 1] == "-1"
    assert args.count("-i") == 3                      # 2 voces + música (la diapositiva muda no aporta audio)


@needs_ffmpeg
def test_real_mix_loops_music_ducks_and_limits(tmp_path):
    tl = tl_with_audio(tmp_path, [2.0, 2.0])
    music = tone(tmp_path / "music.wav", 1.0, freq=110, amp=0.9)    # más corta que el vídeo → se repite
    out = tmp_path / "mix.wav"
    cfg = {"music_volume": 0.8, "voice_volume": 1.0, "ducking": True, "ducking_strength": 1.0,
           "fade_in": 0.2, "fade_out": 0.2}
    stats = audio_mixer.mix("ffmpeg", tl, cfg, out, music)
    a, sr = read_wav(out)
    assert abs(a.size / sr - tl.total) < 0.03
    assert not stats.silent and stats.peak_db <= -0.5
    # La música suena cuando no hay voz (entre narraciones) — no está vacía
    gap_t = tl.items[0].narration_end + 0.1
    gap = a[int(gap_t * sr): int((gap_t + 0.1) * sr)]
    assert np.sqrt(np.mean(gap ** 2)) > 0.01


@needs_ffmpeg
def test_mix_without_music_and_without_voice(tmp_path):
    tl = tl_with_audio(tmp_path, [0.0, 0.0])
    out = tmp_path / "silence.wav"
    args = audio_mixer.build_mix_command("ffmpeg", tl, {}, out, None)
    subprocess.run(args, check=True)
    a, sr = read_wav(out)
    assert abs(a.size / sr - tl.total) < 0.03


def test_final_command_structure(tmp_path):
    tl = tl_with_audio(tmp_path, [1.0, 1.0, 1.0])
    spec = OutputSpec(1080, 1920, 30, 20, "medium", "192k", False)
    clips = [tmp_path / f"c{i}.mp4" for i in range(3)]
    args = build_final_command("ffmpeg", clips, tl, tmp_path / "a.wav", tmp_path / "out.mp4", spec,
                               "subs.ass", "fonts", 0.3)
    graph = args[args.index("-filter_complex") + 1]
    assert graph.count("xfade=transition=fade") == 2
    assert f"offset={tl.items[1].start:.4f}" in graph and f"offset={tl.items[2].start:.4f}" in graph
    assert "ass=subs.ass:fontsdir=fonts" in graph
    for flag, val in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-c:a", "aac"), ("-crf", "20"),
                      ("-preset", "medium"), ("-movflags", "+faststart")):
        assert args[args.index(flag) + 1] == val
    assert args[args.index("-t") + 1] == f"{tl.total:.3f}"
    # Sin transición → concat
    tl2 = build_timeline([{"kind": "slide", "audio_path": None, "audio_duration": 0}] * 2,
                         **{**BASE, "transition": "none"})
    args2 = build_final_command("ffmpeg", clips[:2], tl2, tmp_path / "a.wav", tmp_path / "o.mp4", spec, None, None)
    assert "concat=n=2" in args2[args2.index("-filter_complex") + 1]


def test_clip_commands_are_argument_lists(tmp_path):
    weird = tmp_path / "carpeta con espacios & símbolos" / "frame.png"
    a = build_static_clip_command("ffmpeg", weird, 60, 30, tmp_path / "o.mp4")
    assert str(weird) in a and "-loop" in a
    b = build_pipe_clip_command("ffmpeg", 540, 960, 30, 60, tmp_path / "o.mp4")
    assert b[b.index("-s") + 1] == "540x960" and b[b.index("-frames:v") + 1] == "60"
