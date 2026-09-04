"""
Domain model for the demo bank.

This is deliberately small but shaped like the real thing: customers own
accounts, accounts own cards, a card block produces a ticket, and a ticket
produces an out-of-band notification. Every tool call the voice agent makes is
written to ToolCallLog so the agent's behaviour is auditable after the call.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Customer(TimestampedModel):
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=32, help_text="E.164, e.g. +41791234567")
    email = models.EmailField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    national_id_last4 = models.CharField(max_length=4, blank=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name

    @property
    def first_name(self):
        return self.full_name.split(" ")[0] if self.full_name else ""


class Account(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"
        CLOSED = "closed", "Closed"

    class Kind(models.TextChoices):
        CHECKING = "checking", "Checking"
        SAVINGS = "savings", "Savings"

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="accounts"
    )
    account_number = models.CharField(max_length=20, unique=True, db_index=True)
    account_type = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.CHECKING
    )
    # Never stored in the clear: Django's password hashers are reused for PINs.
    pin_hash = models.CharField(max_length=256)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    failed_pin_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["account_number"]

    def __str__(self):
        return f"{self.account_number} ({self.customer.full_name})"

    # -- PIN handling ------------------------------------------------------
    def set_pin(self, raw_pin):
        self.pin_hash = make_password(str(raw_pin))

    def check_pin(self, raw_pin):
        return check_password(str(raw_pin), self.pin_hash)

    # -- lockout -----------------------------------------------------------
    @property
    def is_locked(self):
        return self.locked_until is not None and self.locked_until > timezone.now()

    @property
    def lock_seconds_remaining(self):
        if not self.is_locked:
            return 0
        return int((self.locked_until - timezone.now()).total_seconds())

    def register_failed_attempt(self):
        self.failed_pin_attempts += 1
        if self.failed_pin_attempts >= settings.MAX_PIN_ATTEMPTS:
            self.locked_until = timezone.now() + timedelta(
                minutes=settings.LOCKOUT_MINUTES
            )
        self.save(update_fields=["failed_pin_attempts", "locked_until", "updated_at"])

    def reset_failed_attempts(self):
        if self.failed_pin_attempts or self.locked_until:
            self.failed_pin_attempts = 0
            self.locked_until = None
            self.save(
                update_fields=["failed_pin_attempts", "locked_until", "updated_at"]
            )

    @property
    def masked_number(self):
        return f"****{self.account_number[-4:]}"


class Card(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        BLOCKED = "blocked", "Blocked"

    class Kind(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="cards"
    )
    brand = models.CharField(max_length=20, default="Visa")
    card_type = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.DEBIT
    )
    last4 = models.CharField(max_length=4)
    expiry_month = models.PositiveSmallIntegerField(default=12)
    expiry_year = models.PositiveSmallIntegerField(default=2030)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["account__account_number", "last4"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "last4"], name="unique_last4_per_account"
            )
        ]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return f"{self.brand} {self.get_card_type_display().lower()} ending {self.last4}"


class AuthSession(TimestampedModel):
    """
    Proof that the caller passed authentication.

    The voice agent receives an opaque token and must present it on every
    subsequent tool call. Authorisation is therefore enforced here, server side,
    and not by the LLM's prompt -- an agent that "decides" it already verified
    the caller still cannot read a balance without a valid token.
    """

    class Level(models.TextChoices):
        BASIC = "basic", "Basic (account + PIN)"
        ELEVATED = "elevated", "Elevated (PIN + OTP)"

    token = models.CharField(max_length=64, unique=True, db_index=True)
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="auth_sessions"
    )
    level = models.CharField(
        max_length=16, choices=Level.choices, default=Level.BASIC
    )
    caller_id = models.CharField(max_length=32, blank=True)
    channel = models.CharField(max_length=32, default="voice")
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.token[:8]}... -> {self.account.account_number}"

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    @property
    def seconds_remaining(self):
        return max(0, int((self.expires_at - timezone.now()).total_seconds()))


class OtpChallenge(TimestampedModel):
    """One-time code used as step-up authentication before a card is blocked."""

    session = models.ForeignKey(
        AuthSession, on_delete=models.CASCADE, related_name="otp_challenges"
    )
    code_hash = models.CharField(max_length=256)
    purpose = models.CharField(max_length=32, default="card_block")
    destination = models.CharField(max_length=120, blank=True)
    channel = models.CharField(max_length=16, default="sms")
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def set_code(self, raw_code):
        self.code_hash = make_password(str(raw_code))

    def check_code(self, raw_code):
        return check_password(str(raw_code), self.code_hash)

    @property
    def is_open(self):
        return (
            self.consumed_at is None
            and self.expires_at > timezone.now()
            and self.attempts < settings.MAX_OTP_ATTEMPTS
        )


class Ticket(TimestampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        FAILED = "failed", "Failed"

    class Reason(models.TextChoices):
        LOST = "lost", "Lost"
        STOLEN = "stolen", "Stolen"
        COMPROMISED = "compromised", "Compromised / fraud suspected"
        DAMAGED = "damaged", "Damaged"
        OTHER = "other", "Other"

    ticket_number = models.CharField(max_length=32, unique=True, db_index=True)
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="tickets"
    )
    card = models.ForeignKey(
        Card, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    category = models.CharField(max_length=32, default="card_block")
    reason = models.CharField(
        max_length=32, choices=Reason.choices, default=Reason.OTHER
    )
    incident_description = models.TextField(blank=True)
    incident_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN
    )
    opened_via_session = models.ForeignKey(
        AuthSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.ticket_number


class Notification(TimestampedModel):
    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    destination = models.CharField(max_length=120)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    provider = models.CharField(max_length=32, default="console")
    provider_message_id = models.CharField(max_length=120, blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} -> {self.destination} ({self.status})"


class ToolCallLog(TimestampedModel):
    """
    One row per tool invocation from the voice agent.

    Secrets (PIN, OTP, session tokens, API keys) are redacted before the payload
    is stored -- see api.views.REDACTED_KEYS.
    """

    tool_name = models.CharField(max_length=64, db_index=True)
    ok = models.BooleanField(default=False)
    http_status = models.PositiveSmallIntegerField(default=200)
    error_code = models.CharField(max_length=48, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_calls",
    )
    session_token_prefix = models.CharField(max_length=12, blank=True)
    remote_addr = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "tool call log"
        verbose_name_plural = "tool call logs"

    def __str__(self):
        return f"{self.tool_name} {'ok' if self.ok else 'fail'} {self.created_at:%H:%M:%S}"


class ProviderSettings(TimestampedModel):
    """
    Editable credentials for the outbound email/SMS providers.

    A single row (pk=1). It is created on first use from the environment, so a
    container deployed with BREVO_API_KEY et al. works with no clicking, and an
    operator can still change providers from the dashboard without a redeploy.

    Secrets live in this table in the clear. That is the same trust boundary as
    the .env file they came from -- anyone with database access has them either
    way -- but it does mean the database is now credential material: back it up
    accordingly and keep the dashboard behind staff login. Secrets are never
    rendered back into the page; the form only ever writes them.
    """

    class EmailProvider(models.TextChoices):
        CONSOLE = "console", "Console (stored, not sent)"
        SMTP = "smtp", "SMTP"
        BREVO = "brevo", "Brevo (API)"

    class SmsProvider(models.TextChoices):
        CONSOLE = "console", "Console (stored, not sent)"
        BREVO = "brevo", "Brevo (API)"
        TWILIO = "twilio", "Twilio"

    SECRET_FIELDS = ("brevo_api_key", "smtp_password", "twilio_auth_token")

    singleton_id = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)

    email_provider = models.CharField(
        max_length=16, choices=EmailProvider.choices, default=EmailProvider.CONSOLE
    )
    sms_provider = models.CharField(
        max_length=16, choices=SmsProvider.choices, default=SmsProvider.CONSOLE
    )
    default_channel = models.CharField(
        max_length=16, choices=Notification.Channel.choices, default=Notification.Channel.SMS
    )

    # -- Brevo (one API key covers both transactional email and SMS) --------
    brevo_api_key = models.CharField(max_length=256, blank=True)
    brevo_sender_email = models.EmailField(blank=True)
    brevo_sender_name = models.CharField(max_length=80, blank=True, default="AIDA Bank")
    brevo_sms_sender = models.CharField(
        max_length=11,
        blank=True,
        default="AIDABank",
        help_text="Alphanumeric sender shown on the SMS. Max 11 characters.",
    )

    # -- SMTP ---------------------------------------------------------------
    smtp_host = models.CharField(max_length=120, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_user = models.CharField(max_length=200, blank=True)
    smtp_password = models.CharField(max_length=256, blank=True)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_from_email = models.EmailField(blank=True)

    # -- Twilio -------------------------------------------------------------
    twilio_account_sid = models.CharField(max_length=64, blank=True)
    twilio_auth_token = models.CharField(max_length=128, blank=True)
    twilio_from_number = models.CharField(max_length=32, blank=True)

    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "provider settings"
        verbose_name_plural = "provider settings"

    def __str__(self):
        return f"email={self.email_provider} sms={self.sms_provider}"

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls):
        """Fetch the row, seeding it from the environment the first time."""
        instance = cls.objects.filter(singleton_id=1).first()
        if instance is not None:
            return instance
        return cls.objects.create(singleton_id=1, **cls.defaults_from_env())

    @staticmethod
    def defaults_from_env():
        """
        Seed values from settings.py (which read them from the environment).

        NOTIFICATION_PROVIDER is honoured as a per-channel default so an .env
        written before this table existed keeps behaving the same way.
        """
        legacy = (settings.NOTIFICATION_PROVIDER or "console").lower()
        email_default = settings.EMAIL_PROVIDER or (
            legacy if legacy in {"smtp", "brevo"} else "console"
        )
        sms_default = settings.SMS_PROVIDER or (
            legacy if legacy in {"twilio", "brevo"} else "console"
        )
        return {
            "email_provider": email_default,
            "sms_provider": sms_default,
            "default_channel": settings.DEFAULT_NOTIFICATION_CHANNEL or "sms",
            "brevo_api_key": settings.BREVO_API_KEY,
            "brevo_sender_email": settings.BREVO_SENDER_EMAIL or "",
            "brevo_sender_name": settings.BREVO_SENDER_NAME or "AIDA Bank",
            "brevo_sms_sender": settings.BREVO_SMS_SENDER or "AIDABank",
            "smtp_host": settings.EMAIL_HOST,
            "smtp_port": settings.EMAIL_PORT or 587,
            "smtp_user": settings.EMAIL_HOST_USER,
            "smtp_password": settings.EMAIL_HOST_PASSWORD,
            "smtp_use_tls": settings.EMAIL_USE_TLS,
            "smtp_from_email": settings.DEFAULT_FROM_EMAIL or "",
            "twilio_account_sid": settings.TWILIO_ACCOUNT_SID,
            "twilio_auth_token": settings.TWILIO_AUTH_TOKEN,
            "twilio_from_number": settings.TWILIO_FROM_NUMBER,
        }

    # -- readiness ---------------------------------------------------------
    def provider_for(self, channel):
        return (
            self.email_provider
            if channel == Notification.Channel.EMAIL
            else self.sms_provider
        )

    def missing_fields_for(self, channel):
        """Which required fields are still blank for the selected provider."""
        provider = self.provider_for(channel)
        required = {
            ("email", "brevo"): ["brevo_api_key", "brevo_sender_email"],
            ("email", "smtp"): ["smtp_host", "smtp_from_email"],
            ("sms", "brevo"): ["brevo_api_key", "brevo_sms_sender"],
            ("sms", "twilio"): [
                "twilio_account_sid",
                "twilio_auth_token",
                "twilio_from_number",
            ],
        }.get((channel, provider), [])
        return [name for name in required if not getattr(self, name)]

    def is_ready(self, channel):
        return not self.missing_fields_for(channel)


class TunnelSettings(TimestampedModel):
    """
    How this service works out the public URL it advertises to AIDA.

    A single row (pk=1), like ProviderSettings. Kept separate because it is
    operational routing rather than credentials -- nothing here is secret.
    """

    class Mode(models.TextChoices):
        AUTO = "auto", "Auto-detect from the ngrok agent"
        MANUAL = "manual", "Manual URL"
        OFF = "off", "Off (use PUBLIC_BASE_URL from the environment)"

    SECRET_FIELDS = ("ngrok_authtoken",)

    singleton_id = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.AUTO)
    # Stored so the agent can be started from the dashboard without editing
    # .env and rebuilding. Seeded from NGROK_AUTHTOKEN on first load.
    ngrok_authtoken = models.CharField(max_length=256, blank=True)
    agent_api_url = models.CharField(
        max_length=200,
        blank=True,
        help_text="ngrok agent API. http://ngrok:4040/api/tunnels inside compose.",
    )
    manual_url = models.URLField(
        blank=True,
        help_text="Used when mode is Manual. E.g. a cloudflared URL or a deployed host.",
    )
    # Last successfully detected address, so a page still renders something
    # useful during the seconds an agent is restarting.
    detected_url = models.URLField(blank=True)
    detected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "tunnel settings"
        verbose_name_plural = "tunnel settings"

    def __str__(self):
        return f"{self.mode}: {self.effective_hint or 'not resolved'}"

    @classmethod
    def load(cls):
        instance = cls.objects.filter(singleton_id=1).first()
        if instance is not None:
            return instance
        return cls.objects.create(
            singleton_id=1,
            mode=cls.Mode.AUTO,
            agent_api_url=settings.NGROK_API_URL,
            ngrok_authtoken=settings.NGROK_AUTHTOKEN,
        )

    def remember(self, url):
        """Cache a freshly detected URL, writing only when it actually changed."""
        url = (url or "").rstrip("/")
        if url and url != self.detected_url:
            self.detected_url = url
            self.detected_at = timezone.now()
            self.save(update_fields=["detected_url", "detected_at", "updated_at"])
        elif url:
            self.detected_at = timezone.now()
            self.save(update_fields=["detected_at", "updated_at"])

    @property
    def effective_hint(self):
        if self.mode == self.Mode.MANUAL:
            return self.manual_url
        return self.detected_url


class SecuritySettings(TimestampedModel):
    """
    Security policy that an operator is allowed to change at runtime.

    Deliberately narrow. The rest of the security configuration -- API keys, PIN
    attempt limits, lockout duration, session TTL -- stays in the environment and
    read-only in the UI, so the web interface cannot be used to weaken the
    system's own defences. Step-up is the exception because it is a genuine
    operational choice: on for anything resembling production, off for a
    continuous demo call where an SMS round-trip would stall the conversation.

    Every change records who made it and when, because "who turned off the
    second factor" is a question worth being able to answer.
    """

    singleton_id = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    require_otp_for_card_block = models.BooleanField(
        default=False,
        help_text="Require a one-time code before a card can be blocked.",
    )
    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "security settings"
        verbose_name_plural = "security settings"

    def __str__(self):
        state = "on" if self.require_otp_for_card_block else "off"
        return f"card-block step-up: {state}"

    @classmethod
    def load(cls):
        """Fetch the row, seeding it from the environment the first time."""
        instance = cls.objects.filter(singleton_id=1).first()
        if instance is not None:
            return instance
        return cls.objects.create(
            singleton_id=1,
            require_otp_for_card_block=settings.REQUIRE_OTP_FOR_CARD_BLOCK,
        )

    @classmethod
    def otp_required_for_card_block(cls):
        return cls.load().require_otp_for_card_block
