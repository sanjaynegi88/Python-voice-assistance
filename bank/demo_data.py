"""
The demo bank's seed data, in one place.

It lives here rather than inside the management command so the dashboard can
read it too. PINs are stored hashed on the Account row and cannot be read back,
so the operations console shows the *seed* PIN from this table and checks it
against the stored hash -- telling you both what to type and whether it still
works.

Nothing here is a credential for anything real. These are fictional customers.
"""
from datetime import date
from decimal import Decimal

from bank.models import Account, Card


DEMO_DATA = [
    {
        "customer": {
            "full_name": "Priya Raman",
            "phone": "+41791112233",
            "email": "priya.raman@example.com",
            "date_of_birth": date(1988, 3, 14),
            "national_id_last4": "8842",
        },
        "account": {
            "account_number": "1000000001",
            "account_type": Account.Kind.CHECKING,
            "pin": "4821",
            "balance": Decimal("4250.75"),
            "currency": "USD",
            "status": Account.Status.ACTIVE,
        },
        "cards": [
            {"brand": "Visa", "card_type": Card.Kind.DEBIT, "last4": "4417"},
            {"brand": "Mastercard", "card_type": Card.Kind.CREDIT, "last4": "9032"},
        ],
    },
    {
        "customer": {
            "full_name": "Marcus Bennet",
            "phone": "+41794445566",
            "email": "marcus.bennet@example.com",
            "date_of_birth": date(1975, 11, 2),
            "national_id_last4": "1190",
        },
        "account": {
            "account_number": "1000000002",
            "account_type": Account.Kind.CHECKING,
            "pin": "7391",
            "balance": Decimal("128.40"),
            "currency": "USD",
            "status": Account.Status.ACTIVE,
        },
        "cards": [
            {"brand": "Visa", "card_type": Card.Kind.DEBIT, "last4": "6712"},
        ],
    },
    {
        "customer": {
            "full_name": "Elena Fischer",
            "phone": "+41797778899",
            "email": "elena.fischer@example.com",
            "date_of_birth": date(1992, 7, 21),
            "national_id_last4": "3307",
        },
        "account": {
            "account_number": "1000000003",
            "account_type": Account.Kind.SAVINGS,
            "pin": "5560",
            "balance": Decimal("92310.00"),
            "currency": "CHF",
            "status": Account.Status.ACTIVE,
        },
        "cards": [
            {"brand": "Visa", "card_type": Card.Kind.DEBIT, "last4": "2285"},
        ],
    },
    {
        # Added for AIDA voice testing. The account number matters here: unlike
        # the 10000000xx block above, its digits vary, so a speech-to-text
        # engine has something to latch onto. A run of identical digits is the
        # thing ASR reliably miscounts.
        "customer": {
            "full_name": "Sam Wilson",
            "phone": "+41793334455",
            "email": "sam.wilson@example.com",
            "date_of_birth": date(1985, 6, 9),
            "national_id_last4": "5512",
        },
        "account": {
            "account_number": "5645342376",
            "account_type": Account.Kind.CHECKING,
            "pin": "7284",
            "balance": Decimal("15780.25"),
            "currency": "USD",
            "status": Account.Status.ACTIVE,
        },
        "cards": [
            {"brand": "Visa", "card_type": Card.Kind.DEBIT, "last4": "8163"},
        ],
    },
    {
        # Deliberately unusable: demonstrates the ACCOUNT_INACTIVE failure path.
        "customer": {
            "full_name": "Tomas Duarte",
            "phone": "+41792223344",
            "email": "tomas.duarte@example.com",
            "date_of_birth": date(1969, 1, 30),
            "national_id_last4": "7754",
        },
        "account": {
            "account_number": "1000000004",
            "account_type": Account.Kind.CHECKING,
            "pin": "1122",
            "balance": Decimal("0.00"),
            "currency": "USD",
            "status": Account.Status.FROZEN,
        },
        "cards": [
            {"brand": "Visa", "card_type": Card.Kind.DEBIT, "last4": "5031"},
        ],
    },
]


def seed_pin_for(account_number):
    """The PIN this account was seeded with, or "" if it isn't seeded data."""
    for entry in DEMO_DATA:
        if entry["account"]["account_number"] == account_number:
            return entry["account"]["pin"]
    return ""
