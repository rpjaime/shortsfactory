import pytest

from timeline import build_timeline, validate_timeline

BASE = dict(fps=30, transition="fade", transition_duration=0.5, min_slide_duration=2.0, cover_duration=2.5,
            lead_margin=0.15, pause_between=0.4, silent_duration=2.5)


def entries(durs):
    return [{"kind": "slide", "slide_id": str(i), "image": f"{i}.png", "audio_path": f"{i}.wav" if d else None,
             "audio_duration": d, "segments": []} for i, d in enumerate(durs)]


def test_durations_follow_real_audio():
    tl = build_timeline(entries([3.0, 6.2, 1.1, 4.4]), **BASE)
    slots = [it.slot for it in tl.items]
    assert len(set(slots)) > 1                       # no todas iguales
    assert slots[1] > slots[0] > slots[2] or slots[1] > slots[3]
    assert validate_timeline(tl) == []


def test_overlaps_not_double_counted():
    tl = build_timeline(entries([2.0, 2.0, 2.0]), **BASE)
    clip_sum = sum(it.clip_duration for it in tl.items)
    trans_sum = sum(it.transition_out for it in tl.items)
    assert abs(tl.total - (clip_sum - trans_sum)) < 1e-9
    assert abs(tl.total - sum(it.slot for it in tl.items)) < 1e-9
    assert tl.xfade_offsets() == [tl.items[1].start, tl.items[2].start]


def test_audio_never_cut_or_overlapping_transitions():
    tl = build_timeline(entries([7.3, 0.4, 5.55]), **{**BASE, "target_duration": 5})
    for it in tl.items:
        assert it.narration_start >= it.start + it.transition_in - 1e-9
        assert it.narration_end <= it.start + it.slot + 1e-9
    assert tl.items[-1].narration_end <= tl.total
    assert any("supera la objetivo" in w for w in tl.warnings)


def test_frame_quantization():
    tl = build_timeline(entries([1.234, 2.345]), **BASE)
    for it in tl.items:
        assert abs(it.start * 30 - round(it.start * 30)) < 1e-6
        assert abs(it.slot * 30 - round(it.slot * 30)) < 1e-6


def test_min_and_cover_duration_and_silent():
    tl = build_timeline(entries([0.3, 0.0, 0.3]), **BASE)
    assert tl.items[0].slot >= 2.5
    assert tl.items[1].narration_start is None and tl.items[1].slot >= 2.5
    assert tl.items[2].slot >= 2.0


def test_no_transition():
    tl = build_timeline(entries([2.0, 2.0]), **{**BASE, "transition": "none"})
    assert all(it.transition_in == 0 and it.transition_out == 0 for it in tl.items)
    assert tl.transition == "none"


def test_fill_to_target():
    tl = build_timeline(entries([2.0, 2.0, 2.0]), **{**BASE, "target_duration": 20, "fill_to_target": True})
    assert abs(tl.total - 20) < 0.05
    assert validate_timeline(tl) == []


def test_video_fade_does_not_cover_last_sentence():
    tl = build_timeline(entries([2.0, 2.0]), **{**BASE, "video_fade": 0.5})
    last = tl.items[-1]
    assert tl.total - last.narration_end >= 0.5


def test_errors():
    with pytest.raises(ValueError):
        build_timeline([], **BASE)
