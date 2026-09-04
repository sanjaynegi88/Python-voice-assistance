"""Unit tests for the speech helpers -- the part a TTS engine is fussy about."""
from django.test import TestCase

from bank import humanize


class AmountToSpeechTests(TestCase):
    def test_whole_amount(self):
        self.assertEqual(humanize.amount_to_speech("128.00", "USD"), "one hundred and twenty-eight dollars")

    def test_amount_with_cents(self):
        self.assertEqual(
            humanize.amount_to_speech("4250.75", "USD"),
            "four thousand two hundred and fifty dollars and seventy-five cents",
        )

    def test_large_amount_in_another_currency(self):
        self.assertEqual(
            humanize.amount_to_speech("92310.00", "CHF"),
            "ninety-two thousand three hundred and ten francs",
        )

    def test_singular_units(self):
        self.assertEqual(humanize.amount_to_speech("1.01", "USD"), "one dollar and one cent")

    def test_zero(self):
        self.assertEqual(humanize.amount_to_speech("0.00", "USD"), "zero dollars")

    def test_unknown_currency_falls_back_to_the_code(self):
        self.assertEqual(humanize.amount_to_speech("2.00", "XYZ"), "two XYZ")


class ReferenceSpeechTests(TestCase):
    def test_digits_are_read_one_by_one(self):
        self.assertEqual(humanize.digits_to_speech("4417"), "four four one seven")

    def test_ticket_number_is_spelled_out(self):
        self.assertEqual(
            humanize.spell_out_reference("TKT-260904-K7QF2M"),
            "T K T, dash, two six zero nine zero four, dash, K seven Q F two M",
        )

    def test_masking(self):
        self.assertEqual(humanize.mask_destination("+41791112233", "sms"), "+41*****2233")
        self.assertEqual(
            humanize.mask_destination("priya.raman@example.com", "email"),
            "pr*********@example.com",
        )
