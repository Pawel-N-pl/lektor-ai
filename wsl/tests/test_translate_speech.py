"""Which English text `speech` aligns against once a film was translated from English.

A film translated from English already has its verbatim English subtitle on disk, on the exact grid
the Polish cues inherit, so it is a ready-made alignment reference. The one wrinkle is that the
translation prefers the PLAIN dialogue track and alignment prefers SDH, so an SDH stream in the film
still wins when there is one.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import media as M  # noqa: E402
from lektorlib import speech as SP  # noqa: E402

PATTERNS = ["subs/{tag}.eng3.srt", "subs/{tag}.eng2.srt", "subs/{base}.eng.srt"]


class Cfg:
    def __init__(self, work):
        self.raw = {"speech": {"reference_srt_patterns": PATTERNS}}
        self.work_dir = Path(work)

    def __getitem__(self, k):
        return self.raw[k]


def stream(index, title="", hi=0):
    return M.stream_from_ffprobe({"index": index, "codec_type": "subtitle", "codec_name": "subrip",
                                  "disposition": {"hearing_impaired": hi},
                                  "tags": {"language": "eng", "title": title}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Cfg(self.tmp.name)
        (self.cfg.work_dir / "subs").mkdir()
        self.video = Path(self.tmp.name) / "ep.mkv"

    def sidecar(self, language="eng", sdh=False, stream_index=3, origin="subs"):
        src = self.cfg.work_dir / "subs" / f"ep.src.{language}.srt"
        src.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        data = {"base": "ep", "subtitle_stream": stream_index}
        if origin:
            data.update(subtitle_origin=origin, source_language=language,
                        source_srt=f"subs/{src.name}", source_sdh=sdh)
        (self.cfg.work_dir / "ep.source.json").write_text(json.dumps(data), encoding="utf-8")
        return src


class TestFindReference(Base):
    def test_an_english_translation_source_is_a_candidate(self):
        src = self.sidecar("eng")
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), src)

    def test_any_other_language_is_not(self):
        self.sidecar("ger")
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))

    def test_a_polish_extract_has_none(self):
        self.sidecar("eng", origin=None)
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))

    def test_a_configured_pattern_still_wins(self):
        self.sidecar("eng")
        sdh = self.cfg.work_dir / "subs" / "ep.eng.srt"
        sdh.write_text("x", encoding="utf-8")
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), sdh)


class TestChooseReference(Base):
    def choose(self, pick=None):
        extracted = self.cfg.work_dir / "subs" / "ep.eng.srt"
        calls = []

        def extract(cfg, base, video, pick=None):
            calls.append(pick.index if pick is not None else None)
            extracted.write_text("x", encoding="utf-8")
            return extracted

        with mock.patch.object(SP, "reference_stream", return_value=pick), \
             mock.patch.object(SP, "extract_reference_srt", side_effect=extract):
            got = SP.choose_reference(self.cfg, "ep", self.video)
        return got, calls

    def test_the_plain_source_is_reused_when_the_film_has_no_sdh(self):
        src = self.sidecar("eng", stream_index=3)
        got, calls = self.choose(stream(3, "English"))
        self.assertEqual((got, calls), (src, []))

    def test_an_sdh_stream_the_film_carries_is_preferred_and_extracted(self):
        self.sidecar("eng", stream_index=3)
        got, calls = self.choose(stream(2, "English [SDH]"))
        self.assertEqual(got.name, "ep.eng.srt")
        self.assertEqual(calls, [2])

    def test_a_source_that_already_is_sdh_is_used_as_it_is(self):
        src = self.sidecar("eng", sdh=True, stream_index=2)
        got, calls = self.choose(stream(2, "English [SDH]", hi=1))
        self.assertEqual((got, calls), (src, []))

    def test_nothing_on_disk_extracts_as_before(self):
        got, calls = self.choose(stream(2, "English"))
        self.assertEqual(got.name, "ep.eng.srt")
        self.assertEqual(calls, [None])

    def test_no_video_takes_what_is_on_disk(self):
        src = self.sidecar("eng")
        with mock.patch.object(SP, "reference_stream") as rs:
            self.assertEqual(SP.choose_reference(self.cfg, "ep", None), src)
        rs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
