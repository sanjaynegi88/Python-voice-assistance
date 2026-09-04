"""
Helpers that turn data into strings a text-to-speech engine reads correctly.

TTS engines mangle things like "4250.30 USD" and "TKT-20260904-K7QF2M".
Every tool response therefore carries a spoken variant alongside the raw value,
and the agent is instructed to read the spoken one aloud.
"""
from decimal import Decimal

ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
]
SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]

CURRENCY_WORDS = {
    "USD": ("dollar", "dollars", "cent", "cents"),
    "EUR": ("euro", "euros", "cent", "cents"),
    "CHF": ("franc", "francs", "centime", "centimes"),
    "GBP": ("pound", "pounds", "penny", "pence"),
    "INR": ("rupee", "rupees", "paisa", "paise"),
}

# Letters/digits that survive a phone line. No 0/O, 1/I/L, 5/S, 8/B.
REFERENCE_ALPHABET = "23479ACDEFHJKMNPQRTUVWXYZ"

SPOKEN_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def number_to_words(value):
    """Convert a non-negative integer below one trillion into English words."""
    value = int(value)
    if value < 0:
        return "minus " + number_to_words(-value)
    if value < 20:
        return ONES[value]
    if value < 100:
        tens, rest = divmod(value, 10)
        return TENS[tens] + (f"-{ONES[rest]}" if rest else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        head = f"{ONES[hundreds]} hundred"
        return f"{head} and {number_to_words(rest)}" if rest else head
    for scale_value, scale_name in SCALES:
        if value >= scale_value:
            count, rest = divmod(value, scale_value)
            head = f"{number_to_words(count)} {scale_name}"
            if not rest:
                return head
            joiner = " and " if rest < 100 else " "
            return head + joiner + number_to_words(rest)
    return str(value)  # pragma: no cover -- unreachable for < 1e12


def amount_to_speech(amount, currency="USD"):
    """'4250.30', 'USD' -> 'four thousand two hundred and fifty dollars and thirty cents'."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    negative = amount < 0
    amount = abs(amount)
    units, fraction = divmod(amount, 1)
    units = int(units)
    minor = int((fraction * 100).to_integral_value())

    major_sing, major_plural, minor_sing, minor_plural = CURRENCY_WORDS.get(
        currency.upper(), (currency, currency, "cent", "cents")
    )
    spoken = f"{number_to_words(units)} {major_sing if units == 1 else major_plural}"
    if minor:
        spoken += f" and {number_to_words(minor)} {minor_sing if minor == 1 else minor_plural}"
    return ("minus " if negative else "") + spoken


def digits_to_speech(text, separator=" "):
    """'4821' -> 'four eight two one' so TTS never says 'four thousand'."""
    return separator.join(SPOKEN_DIGITS.get(ch, ch) for ch in str(text))


def spell_out_reference(reference):
    """
    'TKT-2609-K7QF2M' -> 'T K T, dash, two six zero nine, dash, K 7 Q F 2 M'.

    Read back character by character so the caller can write the ticket down.
    """
    parts = []
    for chunk in str(reference).split("-"):
        if not chunk:
            continue
        parts.append(" ".join(SPOKEN_DIGITS.get(ch, ch.upper()) for ch in chunk))
    return ", dash, ".join(parts)


def mask_destination(value, channel):
    """Mask a phone number or email for logs and dashboard display."""
    value = value or ""
    if channel == "email" and "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}{'*' * max(1, len(local) - len(head))}@{domain}"
    if len(value) <= 4:
        return value
    return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}" if len(value) > 7 else value
