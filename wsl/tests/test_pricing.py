"""Offline tests for the corrected ElevenLabs rate table.

The table this replaced charged 0.30/0.24/0.18/0.12 per plan. The real rate is flat per model
family and identical on every plan; the plan only moves the included allowance and the concurrency.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import pricing as P  # noqa: E402


class TestRates(unittest.TestCase):
    def test_flash_and_turbo_are_the_cheap_family(self):
        for m in ("eleven_flash_v2_5", "eleven_flash_v2", "eleven_turbo_v2_5", "eleven_turbo_v2"):
            self.assertTrue(P.is_fast_model(m), m)
            self.assertEqual(P.usd_per_1k(m), 0.05)

    def test_everything_else_is_double(self):
        for m in ("eleven_multilingual_v2", "eleven_v3", ""):
            self.assertFalse(P.is_fast_model(m), m)
            self.assertEqual(P.usd_per_1k(m), 0.10)

    def test_rate_does_not_depend_on_the_plan(self):
        rates = {P.usd_per_1k("eleven_flash_v2_5") for _ in P.PLAN_INCLUDED}
        self.assertEqual(rates, {0.05})

    def test_one_episode_at_18333_chars(self):
        self.assertAlmostEqual(P.tts_usd(18333, "eleven_flash_v2_5"), 0.9167, places=4)
        self.assertAlmostEqual(P.tts_usd(18333, "eleven_multilingual_v2"), 1.8333, places=4)

    def test_plan_limits(self):
        self.assertEqual(P.plan_limits("creator", "eleven_flash_v2_5"), ("creator", 440_000, 10))
        self.assertEqual(P.plan_limits("creator", "eleven_v3"), ("creator", 220_000, 5))
        self.assertEqual(P.plan_limits("CREATOR", "eleven_flash_v2_5")[0], "creator")
        # An unknown plan falls back to the one this account is actually on.
        self.assertEqual(P.plan_limits("enterprise", "eleven_flash_v2_5")[0], "creator")

    def test_a_season_still_fits_the_creator_allowance(self):
        season = 18333 * 12
        _, included, _ = P.plan_limits("creator", "eleven_flash_v2_5")
        self.assertLess(season, included)


if __name__ == "__main__":
    unittest.main()
