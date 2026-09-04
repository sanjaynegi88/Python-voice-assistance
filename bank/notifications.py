"""
Out-of-band notification delivery (requirement 7).

Providers are chosen per channel, from the editable bank.ProviderSettings row:

  email:  console | smtp | brevo
  sms:    console | brevo | twilio

  console -- nothing leaves the machine; the message is persisted and rendered
             on the dashboard's Notifications page. The default, so the demo
             runs with no third-party account.
  brevo   -- Brevo's transactional API. One API key does both email and SMS.
  smtp    -- any SMTP server, including Brevo's relay, via Django's mail layer.
  twilio  -- Twilio's REST API for SMS.

The HTTP providers are called with `requests` directly rather than their SDKs:
two endpoints do not justify two more dependencies, and it keeps what goes over
the wire visible in this file.

Every attempt -- successful or not -- is persisted as a Notification row, so a
delivery failure is visible to the agent and to the dashboard rather than being
silently swallowed.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

from .models import Notification, ProviderSettings

logger = logging.getLogger("toolcall")

TWILIO_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
BREVO_EMAIL_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
BREVO_SMS_ENDPOINT = "https://api.brevo.com/v3/transactionalSMS/sms"


class NotificationError(Exception):
    """Raised when a provider refuses or fails to deliver a message."""


def resolve_destination(customer, channel):
    if channel == Notification.Channel.EMAIL:
        return customer.email
    return customer.phone


def default_channel():
    return ProviderSettings.load().default_channel


def send_notification(*, account, channel, subject, body, ticket=None):
    """
    Deliver a message and record the attempt.

    Returns the persisted Notification. Raises NotificationError when the
    destination is missing or the provider rejects the send; the row is still
    written with status=failed so nothing disappears.
    """
    config = ProviderSettings.load()
    channel = (channel or config.default_channel).lower()
    if channel not in dict(Notification.Channel.choices):
        raise NotificationError(f"Unsupported channel {channel!r}")

    destination = resolve_destination(account.customer, channel)
    provider = config.provider_for(channel)

    notification = Notification.objects.create(
        account=account,
        ticket=ticket,
        channel=channel,
        destination=destination or "",
        subject=subject,
        body=body,
        provider=provider,
        status=Notification.Status.QUEUED,
    )

    if not destination:
        return _fail(notification, f"Customer has no {channel} destination on file.")

    missing = config.missing_fields_for(channel)
    if missing:
        return _fail(
            notification,
            f"{provider} is selected for {channel} but not configured: "
            f"{', '.join(missing)}. Set it under Dashboard -> Settings -> Providers.",
        )

    try:
        message_id = deliver(config, channel, destination, subject, body)
    except NotificationError:
        raise
    except Exception as exc:  # noqa: BLE001 -- provider failures are reported, not raised
        return _fail(notification, str(exc))

    notification.status = Notification.Status.SENT
    notification.provider_message_id = str(message_id or "")
    notification.sent_at = timezone.now()
    notification.save(
        update_fields=["status", "provider_message_id", "sent_at", "updated_at"]
    )
    logger.info(
        "notification sent provider=%s channel=%s id=%s", provider, channel, message_id
    )
    return notification


def _fail(notification, message):
    notification.status = Notification.Status.FAILED
    notification.error = message
    notification.save(update_fields=["status", "error", "updated_at"])
    logger.warning(
        "notification failed provider=%s channel=%s error=%s",
        notification.provider,
        notification.channel,
        message,
    )
    raise NotificationError(message)


def deliver(config, channel, destination, subject, body):
    """Dispatch to the configured provider. Raises on failure, returns a message id."""
    provider = config.provider_for(channel)

    if channel == Notification.Channel.EMAIL:
        if provider == "brevo":
            return _brevo_email(config, destination, subject, body)
        if provider == "smtp":
            return _smtp_email(config, destination, subject, body)
        return _console(channel, destination, subject, body)

    if provider == "brevo":
        return _brevo_sms(config, destination, body)
    if provider == "twilio":
        return _twilio_sms(config, destination, body)
    return _console(channel, destination, subject, body)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
def _console(channel, destination, subject, body):
    logger.info(
        "console notification channel=%s to=%s subject=%s\n%s",
        channel,
        destination,
        subject,
        body,
    )
    return f"console-{timezone.now().timestamp():.0f}"


def _smtp_email(config, destination, subject, body):
    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=config.smtp_host,
        port=config.smtp_port,
        username=config.smtp_user or None,
        password=config.smtp_password or None,
        use_tls=config.smtp_use_tls,
        timeout=settings.PROVIDER_HTTP_TIMEOUT,
    )
    EmailMessage(
        subject=subject,
        body=body,
        from_email=config.smtp_from_email or settings.DEFAULT_FROM_EMAIL,
        to=[destination],
        connection=connection,
    ).send(fail_silently=False)
    return f"smtp-{timezone.now().timestamp():.0f}"


def _brevo_email(config, destination, subject, body):
    payload = {
        "sender": {
            "name": config.brevo_sender_name or "AIDA Bank",
            "email": config.brevo_sender_email,
        },
        "to": [{"email": destination}],
        "subject": subject or "Notification from AIDA Bank",
        "textContent": body,
    }
    data = _brevo_post(config, BREVO_EMAIL_ENDPOINT, payload)
    return data.get("messageId", "")


def _brevo_sms(config, destination, body):
    payload = {
        "sender": (config.brevo_sms_sender or "AIDABank")[:11],
        "recipient": destination,
        "content": body,
        "type": "transactional",
    }
    data = _brevo_post(config, BREVO_SMS_ENDPOINT, payload)
    return data.get("messageId") or data.get("reference", "")


def _brevo_post(config, url, payload):
    import requests  # imported lazily so the console provider needs no network stack

    response = requests.post(
        url,
        headers={
            "api-key": config.brevo_api_key,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json=payload,
        timeout=settings.PROVIDER_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise NotificationError(f"Brevo {response.status_code}: {_brief(response)}")
    try:
        return response.json()
    except ValueError:
        return {}


def _twilio_sms(config, destination, body):
    import requests

    response = requests.post(
        TWILIO_ENDPOINT.format(sid=config.twilio_account_sid),
        auth=(config.twilio_account_sid, config.twilio_auth_token),
        data={
            "To": destination,
            "From": config.twilio_from_number,
            "Body": body,
        },
        timeout=settings.PROVIDER_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise NotificationError(f"Twilio {response.status_code}: {_brief(response)}")
    return response.json().get("sid", "")


def _brief(response):
    """A short, log-safe slice of a provider's error body."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    return str(data.get("message") or data.get("error") or data)[:200]


# ---------------------------------------------------------------------------
# used by the dashboard's "send test" button
# ---------------------------------------------------------------------------
def send_test_message(*, channel, destination, config=None):
    """
    Send a one-off message to an arbitrary destination, bypassing the customer
    lookup. Returns the provider's message id; raises NotificationError.
    """
    config = config or ProviderSettings.load()
    channel = (channel or config.default_channel).lower()
    if channel not in dict(Notification.Channel.choices):
        raise NotificationError(f"Unsupported channel {channel!r}")
    if not destination:
        raise NotificationError("Enter a destination to send the test to.")

    missing = config.missing_fields_for(channel)
    if missing:
        raise NotificationError(
            f"{config.provider_for(channel)} is not fully configured: "
            f"{', '.join(missing)}."
        )

    subject = "AIDA Bank test message"
    body = (
        "This is a test from the AIDA Bank ops console. "
        "If you received it, the provider credentials are working."
    )
    try:
        return deliver(config, channel, destination, subject, body)
    except NotificationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NotificationError(str(exc)) from exc
