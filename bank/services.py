"""
Business logic behind the four voice-agent tools.

The views in `api/` are thin: they parse JSON, call one function from here, and
serialise the result. All authorisation decisions live in this module so they
cannot be talked around by the LLM -- the agent chooses *when* to call a tool,
never *whether* the caller is allowed to.
"""
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import humanize
from .models import (
    Account,
    AuthSession,
    Card,
    Notification,
    OtpChallenge,
    SecuritySettings,
    Ticket,
)
from .notifications import NotificationError, default_channel, send_notification

logger = logging.getLogger("toolcall")


def otp_destination_phrase():
    """
    How to describe where the one-time code will arrive.

    Saying "text it to the number on your account" while the code is going to
    email sends the caller looking in the wrong place, so this follows the
    configured channel rather than assuming SMS.
    """
    if default_channel() == Notification.Channel.EMAIL:
        return "the email address on your account"
    return "the number on your account"


class ToolError(Exception):
    """
    A failure the agent is expected to explain to the caller.

    `speech_hint` is a ready-made sentence for the bot to say, so error handling
    (requirement 8) stays consistent no matter how the LLM phrases things.
    """

    http_status = 400

    def __init__(self, code, message, speech_hint=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.speech_hint = speech_hint or message
        if http_status is not None:
            self.http_status = http_status


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
def authenticate(*, account_number, pin, caller_id="", channel="voice"):
    """Verify account number + PIN and mint a short-lived session token."""
    account_number = str(account_number or "").strip().replace(" ", "")
    pin = str(pin or "").strip().replace(" ", "")

    # A PIN that arrived as a JSON number has already lost any leading zero:
    # {"pin": "0421"} survives, {"pin": 0421} is not even valid JSON, and
    # {"pin": 421} arrives three digits long. PINs here are always four digits,
    # so pad a short all-digit PIN back out rather than failing a caller who
    # typed the right thing into a platform that retyped it as a number.
    if pin.isdigit() and 0 < len(pin) < 4:
        pin = pin.zfill(4)

    if not account_number or not pin:
        raise ToolError(
            "VALIDATION_ERROR",
            "Both account_number and pin are required.",
            "I still need your account number and your PIN before I can continue.",
        )

    try:
        account = Account.objects.select_related("customer").get(
            account_number=account_number
        )
    except Account.DoesNotExist:
        # Same shape of answer as a bad PIN: do not confirm which accounts exist.
        raise ToolError(
            "INVALID_CREDENTIALS",
            "No account matches those credentials.",
            "I couldn't verify those details. Let's try again -- could you repeat "
            "your account number, one digit at a time?",
            http_status=401,
        )

    if account.is_locked:
        raise ToolError(
            "ACCOUNT_LOCKED",
            f"Account locked for another {account.lock_seconds_remaining} seconds.",
            "For your security this account is temporarily locked after too many "
            "failed attempts. Please try again later or visit a branch.",
            http_status=423,
        )

    if account.status != Account.Status.ACTIVE:
        raise ToolError(
            "ACCOUNT_INACTIVE",
            f"Account status is {account.status}.",
            "That account isn't active at the moment, so I can't access it over "
            "the phone. Our branch team can help you with this.",
            http_status=403,
        )

    if not account.check_pin(pin):
        account.register_failed_attempt()
        remaining = max(0, settings.MAX_PIN_ATTEMPTS - account.failed_pin_attempts)
        if remaining == 0:
            raise ToolError(
                "ACCOUNT_LOCKED",
                "Too many failed PIN attempts; account locked.",
                "That PIN didn't match, and I've had to lock the account for your "
                "security. Please call back later or visit a branch.",
                http_status=423,
            )
        raise ToolError(
            "INVALID_CREDENTIALS",
            f"Incorrect PIN. {remaining} attempt(s) remaining.",
            f"That PIN doesn't match our records. You have {remaining} more "
            "attempt(s) -- could you enter or say your four digit PIN again?",
            http_status=401,
        )

    account.reset_failed_attempts()

    session = AuthSession.objects.create(
        token=secrets.token_urlsafe(24),
        account=account,
        level=AuthSession.Level.BASIC,
        caller_id=caller_id or "",
        channel=channel or "voice",
        expires_at=timezone.now()
        + timedelta(seconds=settings.AUTH_SESSION_TTL_SECONDS),
    )
    return session


def resolve_session(token, *, require_level=None):
    """Load a session token, or raise the error the agent should read out."""
    token = (token or "").strip()
    if not token:
        raise ToolError(
            "SESSION_REQUIRED",
            "No session token in the request. Pass the session_token returned by "
            "authenticate_caller (sessionID, sessionToken and token are accepted "
            "too). A caller who authenticated but whose token never reached this "
            "tool will otherwise be asked to verify again in a loop.",
            "Before I can do that I need to verify who you are. Could you give me "
            "your account number and PIN?",
            http_status=401,
        )

    try:
        session = AuthSession.objects.select_related("account", "account__customer").get(
            token=token
        )
    except AuthSession.DoesNotExist:
        raise ToolError(
            "SESSION_INVALID",
            "Unknown session token.",
            "I've lost track of your verification. Let's redo it quickly -- what's "
            "your account number?",
            http_status=401,
        )

    if not session.is_valid:
        raise ToolError(
            "SESSION_EXPIRED",
            "Session expired or revoked.",
            "Your verification has timed out for security. Could you confirm your "
            "account number and PIN once more?",
            http_status=401,
        )

    if require_level == AuthSession.Level.ELEVATED and session.level != (
        AuthSession.Level.ELEVATED
    ):
        raise ToolError(
            "OTP_REQUIRED",
            "This action requires step-up verification.",
            "For this change I need to send you a one-time code first. Shall I "
            f"send it to {otp_destination_phrase()}?",
            http_status=403,
        )

    return session


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------
def get_balance(session):
    account = session.account
    return {
        "customer_name": account.customer.full_name,
        "account_number_masked": account.masked_number,
        "account_type": account.get_account_type_display(),
        "balance": f"{account.balance:.2f}",
        "currency": account.currency,
        "balance_spoken": humanize.amount_to_speech(account.balance, account.currency),
        "as_of": timezone.now().isoformat(),
        "speech_hint": (
            f"Your {account.get_account_type_display().lower()} account ending "
            f"{humanize.digits_to_speech(account.account_number[-4:])} has a balance of "
            f"{humanize.amount_to_speech(account.balance, account.currency)}."
        ),
    }


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------
def list_cards(session):
    cards = session.account.cards.all()
    return {
        "cards": [
            {
                "last4": card.last4,
                "last4_spoken": humanize.digits_to_speech(card.last4),
                "brand": card.brand,
                "type": card.get_card_type_display(),
                "status": card.status,
                "label": card.label,
            }
            for card in cards
        ],
        "speech_hint": _cards_speech_hint(cards),
    }


def _cards_speech_hint(cards):
    active = [c for c in cards if c.status == Card.Status.ACTIVE]
    if not cards:
        return "I don't see any cards on this account."
    if not active:
        return "Every card on this account is already blocked."
    if len(active) == 1:
        card = active[0]
        return (
            f"I can see one active card: a {card.brand} {card.get_card_type_display().lower()} "
            f"ending {humanize.digits_to_speech(card.last4)}. Is that the one to block?"
        )
    described = " and ".join(
        f"a {c.brand} {c.get_card_type_display().lower()} ending "
        f"{humanize.digits_to_speech(c.last4)}"
        for c in active
    )
    return f"I can see {len(active)} active cards: {described}. Which one should I block?"


def _select_card(account, last4):
    last4 = (last4 or "").strip().replace(" ", "")
    cards = list(account.cards.all())

    if not last4:
        active = [c for c in cards if c.status == Card.Status.ACTIVE]
        if len(active) == 1:
            return active[0]
        raise ToolError(
            "CARD_NOT_SPECIFIED",
            "card_last4 is required when the account has more than one active card.",
            _cards_speech_hint(cards),
        )

    matches = [c for c in cards if c.last4 == last4]
    if not matches:
        raise ToolError(
            "CARD_NOT_FOUND",
            f"No card ending {last4} on this account.",
            f"I can't find a card ending {humanize.digits_to_speech(last4)} on your "
            f"account. {_cards_speech_hint(cards)}",
            http_status=404,
        )

    card = matches[0]
    if card.status == Card.Status.BLOCKED:
        raise ToolError(
            "CARD_ALREADY_BLOCKED",
            f"Card ending {last4} is already blocked.",
            f"Good news -- the card ending {humanize.digits_to_speech(last4)} is "
            "already blocked, so nothing further is needed.",
            http_status=409,
        )
    return card


# ---------------------------------------------------------------------------
# step-up OTP
# ---------------------------------------------------------------------------
def issue_otp(session, *, channel=None, purpose="card_block"):
    channel = (channel or default_channel()).lower()
    account = session.account
    code = f"{secrets.randbelow(1_000_000):06d}"

    challenge = OtpChallenge(
        session=session,
        purpose=purpose,
        channel=channel,
        destination=account.customer.phone
        if channel == Notification.Channel.SMS
        else account.customer.email,
        expires_at=timezone.now() + timedelta(seconds=settings.OTP_TTL_SECONDS),
    )
    challenge.set_code(code)
    challenge.save()

    try:
        send_notification(
            account=account,
            channel=channel,
            subject="Your verification code",
            body=(
                f"{account.customer.first_name}, your one-time verification code is "
                f"{code}. It expires in {settings.OTP_TTL_SECONDS // 60} minutes. "
                "Never share this code with anyone."
            ),
        )
    except NotificationError as exc:
        raise ToolError(
            "OTP_DELIVERY_FAILED",
            str(exc),
            "I wasn't able to send the verification code to the contact details on "
            "your account. I can raise this with our support team instead.",
            http_status=502,
        ) from exc

    return {
        "otp_sent": True,
        "channel": channel,
        "destination_masked": humanize.mask_destination(challenge.destination, channel),
        "expires_in_seconds": settings.OTP_TTL_SECONDS,
        "speech_hint": (
            f"I've sent a six digit code to the {channel} on your account. "
            "Please read it back to me when it arrives."
        ),
    }


def verify_otp(session, code, *, purpose="card_block"):
    code = (code or "").strip().replace(" ", "")
    challenge = (
        OtpChallenge.objects.filter(session=session, purpose=purpose, consumed_at=None)
        .order_by("-created_at")
        .first()
    )
    if challenge is None:
        raise ToolError(
            "OTP_NOT_ISSUED",
            "No open OTP challenge for this session.",
            "I haven't sent you a code yet. Shall I send one now?",
        )

    if challenge.expires_at <= timezone.now():
        raise ToolError(
            "OTP_EXPIRED",
            "The code has expired.",
            "That code has expired. Would you like me to send a fresh one?",
        )

    if challenge.attempts >= settings.MAX_OTP_ATTEMPTS:
        raise ToolError(
            "OTP_ATTEMPTS_EXCEEDED",
            "Too many incorrect codes.",
            "That's too many incorrect codes, so I've stopped the verification for "
            "your security. Please call us back or visit a branch.",
            http_status=423,
        )

    if not challenge.check_code(code):
        challenge.attempts += 1
        challenge.save(update_fields=["attempts", "updated_at"])
        remaining = max(0, settings.MAX_OTP_ATTEMPTS - challenge.attempts)
        raise ToolError(
            "OTP_INVALID",
            f"Incorrect code. {remaining} attempt(s) remaining.",
            f"That code doesn't match. You have {remaining} more attempt(s) -- "
            "could you read the six digits again?",
            http_status=401,
        )

    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at", "updated_at"])

    session.level = AuthSession.Level.ELEVATED
    session.save(update_fields=["level", "updated_at"])

    return {
        "verified": True,
        "level": session.level,
        "speech_hint": "Thank you, that code is correct.",
    }


# ---------------------------------------------------------------------------
# card block + ticket
# ---------------------------------------------------------------------------
def generate_ticket_number():
    """TKT-YYMMDD-XXXXXX using an alphabet that survives a phone line."""
    stamp = timezone.now().strftime("%y%m%d")
    for _ in range(10):
        suffix = "".join(
            secrets.choice(humanize.REFERENCE_ALPHABET) for _ in range(6)
        )
        candidate = f"TKT-{stamp}-{suffix}"
        if not Ticket.objects.filter(ticket_number=candidate).exists():
            return candidate
    raise ToolError(
        "TICKET_NUMBER_COLLISION",
        "Could not allocate a unique ticket number.",
        "I'm having trouble creating your reference number. Let me put you through "
        "to a colleague who can complete this for you.",
        http_status=500,
    )


def block_card(
    session,
    *,
    card_last4=None,
    reason="other",
    incident_description="",
    incident_date=None,
):
    """
    Requirement 6: block the card, create the ticket, tie it to the account.

    The card update and the ticket insert share one transaction -- a caller is
    never told "your card is blocked" without a ticket to prove it, and never
    handed a ticket number for a card that is still live.
    """
    if SecuritySettings.otp_required_for_card_block() and session.level != (
        AuthSession.Level.ELEVATED
    ):
        raise ToolError(
            "OTP_REQUIRED",
            "Step-up verification is required before blocking a card.",
            "Before I block the card I need to verify you with a one-time code. "
            f"Shall I send it to {otp_destination_phrase()}?",
            http_status=403,
        )

    reason = (reason or "other").strip().lower()
    valid_reasons = dict(Ticket.Reason.choices)
    if reason not in valid_reasons:
        reason = Ticket.Reason.OTHER

    account = session.account
    card = _select_card(account, card_last4)

    with transaction.atomic():
        locked_card = Card.objects.select_for_update().get(pk=card.pk)
        if locked_card.status == Card.Status.BLOCKED:
            raise ToolError(
                "CARD_ALREADY_BLOCKED",
                "Card was blocked by a concurrent request.",
                "That card is already blocked, so there's nothing more to do.",
                http_status=409,
            )

        locked_card.status = Card.Status.BLOCKED
        locked_card.blocked_at = timezone.now()
        locked_card.blocked_reason = reason
        locked_card.save(
            update_fields=["status", "blocked_at", "blocked_reason", "updated_at"]
        )

        ticket = Ticket.objects.create(
            ticket_number=generate_ticket_number(),
            account=account,
            card=locked_card,
            category="card_block",
            reason=reason,
            incident_description=incident_description or "",
            incident_date=incident_date or None,
            status=Ticket.Status.OPEN,
            opened_via_session=session,
        )

    logger.info(
        "card blocked account=%s card=****%s ticket=%s reason=%s",
        account.account_number,
        locked_card.last4,
        ticket.ticket_number,
        reason,
    )

    return {
        "blocked": True,
        "ticket_number": ticket.ticket_number,
        "ticket_number_spoken": humanize.spell_out_reference(ticket.ticket_number),
        "card_last4": locked_card.last4,
        "card_label": locked_card.label,
        "reason": reason,
        "customer_name": account.customer.full_name,
        "blocked_at": locked_card.blocked_at.isoformat(),
        "speech_hint": (
            f"Done -- your {locked_card.brand} card ending "
            f"{humanize.digits_to_speech(locked_card.last4)} is now blocked. Your "
            f"reference number is {humanize.spell_out_reference(ticket.ticket_number)}. "
            "I'll send you a written confirmation now."
        ),
    }


# ---------------------------------------------------------------------------
# confirmation message
# ---------------------------------------------------------------------------
def send_ticket_confirmation(session, *, ticket_number, channel=None):
    """Requirement 7: name + ticket number + confirmation, out of band."""
    account = session.account
    try:
        ticket = Ticket.objects.select_related("card").get(
            ticket_number=(ticket_number or "").strip().upper(), account=account
        )
    except Ticket.DoesNotExist:
        raise ToolError(
            "TICKET_NOT_FOUND",
            f"No ticket {ticket_number} on this account.",
            "I can't find that reference on your account, so I haven't sent a "
            "confirmation. Let me check the details with you again.",
            http_status=404,
        )

    channel = (channel or default_channel()).lower()
    customer = account.customer
    card_text = ticket.card.label if ticket.card else "your card"

    subject = f"Card blocked -- reference {ticket.ticket_number}"
    body = (
        f"Dear {customer.full_name},\n\n"
        f"We have blocked your {card_text} as requested during your call.\n"
        f"Reference number: {ticket.ticket_number}\n"
        f"Reason recorded: {ticket.get_reason_display()}\n"
        f"Status: the card can no longer be used for payments or withdrawals.\n\n"
        "A replacement can be ordered in the app or at any branch. If you did not "
        "request this, call us immediately.\n\n"
        "AIDA Bank"
    )

    try:
        notification = send_notification(
            account=account,
            channel=channel,
            subject=subject,
            body=body,
            ticket=ticket,
        )
    except NotificationError as exc:
        # Requirement 8: the block itself stands even if the message fails.
        raise ToolError(
            "NOTIFICATION_FAILED",
            str(exc),
            "Your card is blocked and the reference is "
            f"{humanize.spell_out_reference(ticket.ticket_number)}, but I couldn't "
            "send the written confirmation just now. Please write that reference "
            "down -- the block is already active.",
            http_status=502,
        ) from exc

    return {
        "sent": True,
        "channel": notification.channel,
        "destination_masked": humanize.mask_destination(
            notification.destination, notification.channel
        ),
        "ticket_number": ticket.ticket_number,
        "ticket_number_spoken": humanize.spell_out_reference(ticket.ticket_number),
        "customer_name": customer.full_name,
        "provider": notification.provider,
        "provider_message_id": notification.provider_message_id,
        "speech_hint": (
            f"I've sent the confirmation to the {notification.channel} on your "
            f"account. It has your name and the reference "
            f"{humanize.spell_out_reference(ticket.ticket_number)}."
        ),
    }
