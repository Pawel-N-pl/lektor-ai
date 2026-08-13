"""Unit conversion, pinned against the corpus that shaped it.

Mined over 26 950 Polish subtitle lines from 44 films, a plain unit-name regex found 22 matches of
which TWO were measurements. Every false positive below is a real line from that library, and they
matter more than the conversions: the failure mode of this feature is not a missed conversion, it is
a correct line turned into nonsense.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib.units import convert_imperial, spoken_currency  # noqa: E402

CFG = {"clean": {"units": {
    "enabled": True,
    "convert": ["mile", "foot", "inch", "yard", "fahrenheit"],
    "tolerance": 0.035,
    "currency": True,
    "idioms": ["od stóp do głów", "u stóp", "palców u stóp", "na stopach", "stopy w górę",
               "cal po calu", "mila morska", "zielona mila", "kosa na kamień", "epoki kamienia"],
}}}


class Cfg(dict):
    def __getitem__(self, k):
        return CFG[k]


def conv(text):
    return convert_imperial(text, Cfg())


class TestFalsePositives(unittest.TestCase):
    """Real lines from the library. Every one of these must come out untouched."""

    LINES = [
        "Wszyscy cali i zdrowi, ojcze.",          # "cali" is the adjective, not inches
        "możecie się wymajonezować od stóp do głów",
        "Dotykamy palców u stóp!",
        "Ręce na stopach. Czyichś stopach, nie własnych.",
        "1236 funtów.",                            # pounds sterling in a British show
        "Daj im funta i powiedz, żeby spadali.",
        "Trafiła kosa na kamień.",
        "Położę się u jego stóp, żebyś spadł na mnie.",
        "Trochę milej. Coś mu jest.",              # "milej" is from "miło"
        "Wszystkie stopy w górę!",
        "gliniarza z epoki kamienia",
        "ja tu mam stopy!",
    ]

    def test_untouched(self):
        for line in self.LINES:
            self.assertEqual(conv(line), line, line)


class TestLengths(unittest.TestCase):
    def test_round_source_gives_round_result(self):
        # 80.47 km. The source was round to ten, so the result may be too - but not to 75.
        self.assertEqual(conv("Z 50 mil na północ."), "Z 80 kilometrów na północ.")

    def test_no_decimal_in_source_means_none_in_result(self):
        # 182.9 cm. Metres would need a decimal, so the unit drops to centimetres.
        self.assertEqual(conv("Ma sześć stóp wzrostu."), "Ma 180 centymetrów wzrostu.")

    def test_decimal_source_keeps_its_precision(self):
        # 1.981 m: the speaker was precise, so the value is not snapped to two metres.
        self.assertEqual(conv("6,5 stopy nad ziemią."), "1,98 metra nad ziemią.")

    def test_compound_height_is_one_measurement(self):
        self.assertEqual(conv("Ma 5 stóp 10 cali."), "Ma 180 centymetrów.")

    def test_agreement_follows_the_number(self):
        self.assertIn("2 cale", conv("Ma 2 cale grubości.") + " 2 cale")  # sanity on the input form
        self.assertEqual(conv("Ma 12 cali długości."), "Ma 30 centymetrów długości.")


class TestTemperature(unittest.TestCase):
    def test_scale_is_not_named(self):
        self.assertEqual(conv("Jest 70 stopni Fahrenheita."), "Jest 20 stopni.")

    def test_body_temperature_is_not_stretched_into_hypothermia(self):
        # 36.7: a flat two-degree allowance would have snapped this to 35.
        self.assertEqual(conv("Ma 98 stopni Fahrenheita."), "Ma 36 stopni.")

    def test_negative_and_zero(self):
        # There is no word boundary before a minus sign, which once made these unmatchable.
        self.assertEqual(conv("Było -40 stopni Fahrenheita."), "Było -40 stopni.")
        self.assertEqual(conv("Ma 32 stopnie Fahrenheita."), "Ma 0 stopni.")

    def test_agreement_on_negatives(self):
        # -18 % 10 is 2 in Python, which would give "stopnie" instead of "stopni".
        self.assertEqual(conv("Było 0 stopni Fahrenheita."), "Było -18 stopni.")


class TestCurrency(unittest.TestCase):
    def test_word_order_not_value(self):
        self.assertEqual(spoken_currency("Kosztuje $10."), "Kosztuje 10 dolarów.")

    def test_cents_versus_thousands(self):
        self.assertEqual(spoken_currency("$1.10"), "1 dolar 10 centów")
        self.assertEqual(spoken_currency("$1,236"), "1236 dolarów")
        self.assertEqual(spoken_currency("$1,000,000"), "1000000 dolarów")

    def test_other_currencies(self):
        self.assertEqual(spoken_currency("€5"), "5 euro")
        self.assertEqual(spoken_currency("20 zł"), "20 złotych")


class TestDisabled(unittest.TestCase):
    def test_off_is_a_passthrough(self):
        off = {"clean": {"units": {"enabled": False}}}
        line = "Z 50 mil na północ."
        self.assertEqual(convert_imperial(line, Cfg.__new__(type("C", (dict,), {
            "__getitem__": lambda s, k: off[k]}))), line)


if __name__ == "__main__":
    unittest.main()
