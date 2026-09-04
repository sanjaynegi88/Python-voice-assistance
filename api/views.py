"""
The HTTP surface the AIDA agent calls.

Plain Django views returning JsonResponse -- no Django REST Framework, no
FastAPI. Each view is a thin wrapper: parse JSON, call one bank.services
function, serialise. The @tool_endpoint decorator does the cross-cutting work:
API-key check, timing, redaction, ToolCallLog row, structured log line, and a
uniform error envelope.

Response envelope, always:
    {"ok": true,  "tool": "<name>", "data": {...},  "speech_hint": "..."}
    {"ok": false, "tool": "<name>", "error": {"code": "...", "message": "..."},
     "speech_hint": "..."}
"""
import json
import logging
import time
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from bank import services
from bank.models import ProviderSettings, SecuritySettings, ToolCallLog
from bank.services import ToolError

from .auth import api_key_is_valid, extract_api_key
from .schema import TOOLS, function_definitions

logger = logging.getLogger("toolcall")

# The session token under every spelling a platform might use for it. AIDA
# sends "sessionID"; other tools produce camelCase or a bare "token". The value
# is the same opaque string, so accept them all rather than making the caller's
# field naming a source of authentication failures.
SESSION_TOKEN_KEYS = (
    "session_token",
    "sessionToken",
    "sessionID",
    "sessionId",
    "session_id",
    "sessiontoken",
    "token",
)

# Anything matching these keys never reaches the database or the log line.
REDACTED_KEYS = {
    "pin",
    "code",
    "otp",
    "api_key",
    "password",
    "authorization",
    *(key.lower() for key in SESSION_TOKEN_KEYS),
}
# "code" means an OTP in a request, but an error code in a response -- and
# redacting the error code hides the single most useful field when diagnosing a
# failed call. Responses carry no OTP, so it is safe to keep there.
RESPONSE_REDACTED_KEYS = REDACTED_KEYS - {"code"}
REDACTION = "***"

GENERIC_FAILURE_SPEECH = (
    "I'm sorry, our system isn't responding just now. Nothing has been changed on "
    "your account. Please try again in a moment or hold for a colleague."
)


def redact(value, keys=None):
    keys = REDACTED_KEYS if keys is None else keys
    if isinstance(value, dict):
        return {
            key: (REDACTION if key.lower() in keys else redact(item, keys))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, keys) for item in value]
    return value


def redact_response(value):
    return redact(value, RESPONSE_REDACTED_KEYS)


def coerce_scalar(value):
    """
    Normalise a JSON scalar to the string the services layer expects.

    A caller that sends {"pin": 7284} rather than {"pin": "7284"} is not doing
    anything unusual -- LLM tool-calling and low-code platforms both emit bare
    numbers for numeric-looking fields regardless of what the schema says. The
    tools take digit strings, so coerce here rather than making every handler
    defensive.

    Floats are handled because some platforms round-trip integers through a
    float type: 7284.0 must become "7284", not "7284.0".
    """
    if isinstance(value, bool):
        return value  # a real boolean flag, not a stringly-typed number
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    return value


def extract_session_token(payload):
    """First non-empty value among the accepted spellings, matched case-insensitively."""
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for key in SESSION_TOKEN_KEYS:
        value = coerce_scalar(lowered.get(key.lower(), ""))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class ToolContext:
    """Carries per-call state the decorator needs for logging."""

    def __init__(self, request, payload):
        self.request = request
        self.payload = payload
        self.account = None  # set by the handler once the caller is known

    def get(self, key, default=""):
        return coerce_scalar(self.payload.get(key, default))

    def session_token(self):
        """The session token, whatever the caller chose to call the field."""
        return extract_session_token(self.payload)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _envelope_error(tool_name, code, message, speech_hint, status):
    return JsonResponse(
        {
            "ok": False,
            "tool": tool_name,
            "error": {"code": code, "message": message},
            "speech_hint": speech_hint,
        },
        status=status,
    )


def _record_rejection(request, tool_name, detail, started):
    """
    Persist a 401 so it shows up on the dashboard's Tool logs page.

    Without this, a platform that cannot send the API key produces a wall of
    silent 401s that appear nowhere in the app -- which is exactly the failure
    that is hardest to diagnose from the other side of the integration.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            payload = {"_body": "not-an-object"}
    except (ValueError, UnicodeDecodeError):
        payload = {"_body": "unparseable"}

    try:
        ToolCallLog.objects.create(
            tool_name=tool_name,
            ok=False,
            http_status=401,
            error_code="UNAUTHORIZED_CLIENT",
            request_payload=redact(payload),
            response_payload={"error": detail},
            duration_ms=int((time.perf_counter() - started) * 1000),
            remote_addr=_client_ip(request),
        )
    except Exception:  # noqa: BLE001 -- auditing must never break a call
        logger.exception("could not persist rejection log for %s", tool_name)


def tool_endpoint(tool_name):
    """Wrap a handler into a logged, authenticated, JSON tool endpoint."""

    def decorator(handler):
        @csrf_exempt
        @require_POST
        @wraps(handler)
        def view(request, *args, **kwargs):
            started = time.perf_counter()

            if not api_key_is_valid(request):
                _presented, source = extract_api_key(request)
                detail = (
                    "no API key was presented"
                    if source == "none"
                    else f"the key presented in the {source} did not match"
                )
                logger.warning(
                    "tool=%s rejected: %s (from %s)",
                    tool_name,
                    detail,
                    _client_ip(request),
                )
                # Recorded like any other call: a platform that cannot send the
                # key is the single most likely integration failure, and it is
                # invisible on the dashboard if only the header check logs it.
                _record_rejection(request, tool_name, detail, started)
                return _envelope_error(
                    tool_name,
                    "UNAUTHORIZED_CLIENT",
                    f"Missing or invalid API key: {detail}. Send it as an "
                    f"X-API-Key header, an Authorization: Bearer token, an "
                    f"?api_key= query parameter, or an api_key body field.",
                    GENERIC_FAILURE_SPEECH,
                    401,
                )

            try:
                raw = request.body.decode("utf-8") or "{}"
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, UnicodeDecodeError) as exc:
                return _envelope_error(
                    tool_name,
                    "MALFORMED_REQUEST",
                    f"Could not parse JSON body: {exc}",
                    GENERIC_FAILURE_SPEECH,
                    400,
                )

            context = ToolContext(request, payload)
            error_code = ""
            status = 200

            try:
                data = handler(context)
                body = {
                    "ok": True,
                    "tool": tool_name,
                    "data": data,
                    "speech_hint": data.get("speech_hint", ""),
                }
                ok = True
            except ToolError as exc:
                status = exc.http_status
                error_code = exc.code
                body = {
                    "ok": False,
                    "tool": tool_name,
                    "error": {"code": exc.code, "message": exc.message},
                    "speech_hint": exc.speech_hint,
                }
                ok = False
            except Exception as exc:  # noqa: BLE001 -- never leak a stack trace to the caller
                logger.exception("tool=%s crashed", tool_name)
                status = 500
                error_code = "INTERNAL_ERROR"
                body = {
                    "ok": False,
                    "tool": tool_name,
                    "error": {"code": "INTERNAL_ERROR", "message": str(exc)[:200]},
                    "speech_hint": GENERIC_FAILURE_SPEECH,
                }
                ok = False

            duration_ms = int((time.perf_counter() - started) * 1000)
            # Coerced for the same reason as the handler arguments: this runs
            # outside the try/except above, so a numeric token here would
            # escape as a raw 500 instead of the clean error envelope.
            token = extract_session_token(payload)

            logger.info(
                "tool=%s ok=%s status=%s code=%s account=%s session=%s %sms args=%s",
                tool_name,
                ok,
                status,
                error_code or "-",
                context.account.account_number if context.account else "-",
                (token[:8] + "...") if token else "-",
                duration_ms,
                json.dumps(redact(payload), separators=(",", ":")),
            )

            try:
                ToolCallLog.objects.create(
                    tool_name=tool_name,
                    ok=ok,
                    http_status=status,
                    error_code=error_code,
                    request_payload=redact(payload),
                    response_payload=redact_response(body),
                    duration_ms=duration_ms,
                    account=context.account,
                    session_token_prefix=token[:8],
                    remote_addr=_client_ip(request),
                )
            except Exception:  # noqa: BLE001 -- auditing must never break a call
                logger.exception("could not persist ToolCallLog for %s", tool_name)

            return JsonResponse(body, status=status)

        return view

    return decorator


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@tool_endpoint("authenticate_caller")
def authenticate_caller(context):
    session = services.authenticate(
        account_number=context.get("account_number"),
        pin=context.get("pin"),
        caller_id=context.get("caller_id"),
        channel=context.get("channel", "voice") or "voice",
    )
    context.account = session.account
    customer = session.account.customer
    return {
        "authenticated": True,
        "session_token": session.token,
        "customer_name": customer.full_name,
        "first_name": customer.first_name,
        "account_number_masked": session.account.masked_number,
        "account_type": session.account.get_account_type_display(),
        "auth_level": session.level,
        "expires_in_seconds": session.seconds_remaining,
        "speech_hint": (
            f"Thanks {customer.first_name}, you're verified. How can I help you today?"
        ),
    }


@tool_endpoint("get_account_balance")
def get_account_balance(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    return services.get_balance(session)


@tool_endpoint("list_cards")
def list_cards(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    return services.list_cards(session)


@tool_endpoint("send_otp")
def send_otp(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    return services.issue_otp(session, channel=context.get("channel") or None)


@tool_endpoint("verify_otp")
def verify_otp(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    return services.verify_otp(session, context.get("code"))


@tool_endpoint("block_card")
def block_card(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    raw_date = context.get("incident_date")
    return services.block_card(
        session,
        card_last4=context.get("card_last4"),
        reason=context.get("reason", "other"),
        incident_description=context.get("incident_description"),
        incident_date=parse_date(raw_date) if raw_date else None,
    )


@tool_endpoint("send_confirmation")
def send_confirmation(context):
    session = services.resolve_session(context.session_token())
    context.account = session.account
    return services.send_ticket_confirmation(
        session,
        ticket_number=context.get("ticket_number"),
        channel=context.get("channel") or None,
    )


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
@require_GET
def health(request):
    """Unauthenticated liveness probe -- used by docker compose's healthcheck."""
    config = ProviderSettings.load()
    return JsonResponse(
        {
            "ok": True,
            "service": "aida-voice-bank",
            "tools": [tool["name"] for tool in TOOLS],
            "email_provider": config.email_provider,
            "sms_provider": config.sms_provider,
            "email_ready": config.is_ready("email"),
            "sms_ready": config.is_ready("sms"),
            "otp_required_for_card_block": SecuritySettings.otp_required_for_card_block(),
        }
    )


@csrf_exempt
@require_GET
def tool_schema(request):
    """Function-calling definitions, ready to paste into AIDA's tool config."""
    if not api_key_is_valid(request):
        return _envelope_error(
            "tool_schema",
            "UNAUTHORIZED_CLIENT",
            "Missing or invalid X-API-Key header.",
            GENERIC_FAILURE_SPEECH,
            401,
        )
    return JsonResponse({"ok": True, "tools": function_definitions()})
