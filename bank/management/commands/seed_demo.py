"""
Seed the demo bank.

Five customers: four healthy accounts with clearly different balances (the
evaluation checklist asks for at least three), plus one frozen account so the
"graceful failure" path can be demonstrated on camera.

Sam Wilson's account number is deliberately shaped for voice: varied digits,
no long runs. The 10000000xx numbers are realistic but hostile to speech
recognition, which reliably miscounts a row of identical digits.

Idempotent -- re-running updates the rows in place. Pass --reset to wipe the
transactional tables (sessions, tickets, notifications, tool logs) first.
"""
import os
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from bank.demo_data import DEMO_DATA
from bank.models import (
    Account,
    AuthSession,
    Card,
    Customer,
    Notification,
    OtpChallenge,
    Ticket,
    ToolCallLog,
)



class Command(BaseCommand):
    help = "Create the demo customers, accounts, cards and an admin login."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete sessions, tickets, notifications and tool logs first.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            for model in (ToolCallLog, Notification, Ticket, OtpChallenge, AuthSession):
                deleted, _ = model.objects.all().delete()
                self.stdout.write(f"  cleared {model.__name__}: {deleted}")

        for entry in DEMO_DATA:
            customer, _ = Customer.objects.update_or_create(
                full_name=entry["customer"]["full_name"],
                defaults=entry["customer"],
            )
            spec = dict(entry["account"])
            pin = spec.pop("pin")
            account, created = Account.objects.update_or_create(
                account_number=spec.pop("account_number"),
                defaults={**spec, "customer": customer},
            )
            account.set_pin(pin)
            account.failed_pin_attempts = 0
            account.locked_until = None
            account.save()

            for card_spec in entry["cards"]:
                Card.objects.update_or_create(
                    account=account,
                    last4=card_spec["last4"],
                    defaults={
                        **card_spec,
                        "status": Card.Status.ACTIVE,
                        "blocked_at": None,
                        "blocked_reason": "",
                    },
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"  {'created' if created else 'updated'} "
                    f"{account.account_number}  PIN {pin}  "
                    f"{account.balance} {account.currency}  ({customer.full_name})"
                )
            )

        self._ensure_admin()
        self.stdout.write(self.style.SUCCESS("\nSeed complete."))

    def _ensure_admin(self):
        User = get_user_model()
        username = os.getenv("DEMO_ADMIN_USERNAME", "admin")
        password = os.getenv("DEMO_ADMIN_PASSWORD", "admin12345")
        email = os.getenv("DEMO_ADMIN_EMAIL", "admin@aidabank.example")

        user, created = User.objects.get_or_create(
            username=username, defaults={"email": email}
        )
        user.is_staff = True
        user.is_superuser = True
        user.email = email
        user.set_password(password)
        user.save()
        self.stdout.write(
            self.style.SUCCESS(
                f"  dashboard login: {username} / {password} "
                f"({'created' if created else 'password reset'})"
            )
        )
