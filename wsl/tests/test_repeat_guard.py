"""The duplicated-card guard: it must kill the twin and spare the real repetition.

Both halves matter equally. A guard that only proves it removes duplicates would happily pass
while eating "Roy Kent!" shouted twice by two different fans - which is dialogue, not a defect.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lektorlib.srt import Cue, drop_repeated_cards

CFG = {"clean": {"drop_repeated_cards": True, "repeat_overlap_ms": 0}}


def cue(idx, start, end, text):
    return Cue(idx=idx, start_ms=start, end_ms=end, raw=text, text=text)


def test_identical_timestamps_and_text_drops_the_twin():
    # Verbatim from S03E04 cards 54 and 55.
    cues = [cue(54, 239156, 241742, "Jakżeby inaczej. Jakim cudem masz pięć?"),
            cue(55, 239156, 241742, "Jakżeby inaczej. Jakim cudem masz pięć?"),
            cue(56, 241742, 244703, "Jestem schludny.")]
    out, dropped = drop_repeated_cards(cues, CFG)
    assert dropped == [55]
    assert [c.idx for c in out] == [54, 56]


def test_survivor_takes_the_union_of_the_spans():
    cues = [cue(1, 1000, 3000, "Nie wyrobię."), cue(2, 2000, 4500, "Nie wyrobię.")]
    out, dropped = drop_repeated_cards(cues, CFG)
    assert dropped == [2]
    assert (out[0].start_ms, out[0].end_ms) == (1000, 4500)


def test_real_repetition_seconds_apart_survives():
    # S03E02: two fans shouting the same name 3.3 s apart. The lektor must read both.
    cues = [cue(444, 100000, 101000, "Roy Kent!"), cue(446, 104336, 105400, "Roy Kent!")]
    out, dropped = drop_repeated_cards(cues, CFG)
    assert dropped == []
    assert len(out) == 2


def test_touching_cards_are_not_overlapping():
    """The authoring grid butts cards end-to-start; that is not evidence of anything."""
    cues = [cue(1, 1000, 3000, "Tak."), cue(2, 3000, 4000, "Tak.")]
    _, dropped = drop_repeated_cards(cues, CFG)
    assert dropped == []


def test_case_and_whitespace_do_not_hide_a_twin():
    cues = [cue(1, 1000, 3000, "Nie  wyrobię."), cue(2, 1000, 3000, "nie wyrobię.")]
    _, dropped = drop_repeated_cards(cues, CFG)
    assert dropped == [2]


def test_switch_off_restores_the_old_behaviour():
    cues = [cue(1, 1000, 3000, "A."), cue(2, 1000, 3000, "A.")]
    out, dropped = drop_repeated_cards(cues, {"clean": {"drop_repeated_cards": False}})
    assert dropped == [] and len(out) == 2
