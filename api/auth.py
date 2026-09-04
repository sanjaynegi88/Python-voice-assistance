"""
Transport-level authentication for the tool API.

This is *not* the caller's authentication -- it proves the request came from the
AIDA platform rather than from anyone who found the URL. Caller identity is a
separate concern handled by bank.services.authenticate().

The key is accepted four ways, because low-code platforms differ in what they
let you configure per tool. In preference order:

  1. X-API-Key header            -- the default, and the only one that keeps the
                                    key out of URLs and logs
  2. Authorization: Bearer <key> -- for platforms that expose only that header
  3. ?api_key=<key> query string -- for platforms with no header configuration
                                    at all. AIDA's tool definition (url, method,
                                    timeout, response_type, parameters, paths)
                                    has no headers field, so this is the route
                                    that works there: put the key in the URL you
                                    register and it rides along on every call.
  4. "api_key" in the JSON body  -- last resort, for a platform that can send a
                                    constant body field but not a query string

3 and 4 are a real trade-off: a key in a query string ends up in access logs,
proxy logs and the ngrok inspector, where a header would not. It is accepted
here because the alternative on such a platform is no transport auth at all,
which is worse. Prefer the header wherever the platform allows it, and treat a
URL-borne key as rotatable rather than secret -- see README.
"""
import hmac
import json

from django.conf import settings

API_KEY_HEADER = "X-API-Key"
API_KEY_PARAM = "api_key"


def extract_api_key(request):
    """Return the presented key and where it came from, for logging."""
    key = request.headers.get(API_KEY_HEADER, "")
    if key:
        return key.strip(), "header"

    # Some low-code platforms only allow an Authorization header.
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), "bearer"

    # No header configuration available: accept it from the query string.
    key = request.GET.get(API_KEY_PARAM, "")
    if key:
        return key.strip(), "query"

    # Last resort: a constant field in the JSON body.
    key = _key_from_body(request)
    if key:
        return key.strip(), "body"

    return "", "none"


def _key_from_body(request):
    """
    Read api_key out of the JSON body without consuming it.

    request.body is cached by Django, so the view's own json.loads() still sees
    the full payload afterwards.
    """
    if request.content_type and "json" not in request.content_type.lower():
        return ""
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get(API_KEY_PARAM, "")
    return value if isinstance(value, str) else ""


def api_key_is_valid(request):
    presented, _source = extract_api_key(request)
    if not presented:
        return False
    # compare_digest against every configured key so rotation is possible.
    return any(
        hmac.compare_digest(presented, configured)
        for configured in settings.TOOL_API_KEYS
    )
