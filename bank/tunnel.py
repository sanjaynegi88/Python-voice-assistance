"""
Live public URL discovery.

AIDA needs an address it can reach from the internet. In development that comes
from a tunnel, and a tunnel's hostname changes every time it restarts -- which
otherwise means editing .env and restarting the app on every session.

The ngrok agent publishes its current public URL on a local HTTP API, so we ask
it directly and cache the answer. The dashboard then rebuilds every tool
endpoint on that URL, ready to copy into AIDA.

Resolution order for the base URL, most specific first:

  1. a manual URL typed into the dashboard   (mode = manual)
  2. the live ngrok tunnel                   (mode = auto)
  3. PUBLIC_BASE_URL from the environment
  4. the host the dashboard is being browsed on
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger("toolcall")

# Cache the agent lookup: the Settings page can render it several times per
# request cycle, and the tunnel address only changes when the agent restarts.
_CACHE = {"url": None, "checked_at": 0.0, "error": ""}
_CACHE_TTL_SECONDS = 15


class TunnelUnavailable(Exception):
    """The tunnel agent could not be reached or reported no HTTPS tunnel."""


LOCAL_HOSTNAMES = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
    "web", "host.docker.internal",
}


def _is_private_ip(host):
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def is_local_request(request):
    """
    True when the dashboard is being browsed from this machine.

    Used to decide whether the ngrok controls are worth showing at all -- an
    app already reachable on a real hostname has no use for a tunnel -- and,
    more importantly, to refuse to spawn a process for a remote visitor.
    """
    if request is None:
        return False
    try:
        raw_host = request.get_host()
    except Exception:  # noqa: BLE001 -- DisallowedHost is definitionally not local
        return False
    host = raw_host.rsplit(":", 1)[0].strip().lower().strip("[]")
    if host in LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return True
    return _is_private_ip(host)


def agent_candidates(configured=None):
    """
    Addresses to try, in order.

    The agent may be a compose service ("ngrok:4040") or a child process of
    this container ("127.0.0.1:4040"). Trying both means switching between the
    two needs no configuration change.
    """
    seen, ordered = set(), []
    for candidate in (
        configured,
        settings.NGROK_API_URL,
        "http://127.0.0.1:4040/api/tunnels",
        "http://ngrok:4040/api/tunnels",
    ):
        candidate = (candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def detect_ngrok_url(agent_api_url=None, *, force=False):
    """
    Ask the local ngrok agent for its current public HTTPS URL.

    Returns the URL, or raises TunnelUnavailable. Results (including failures)
    are cached briefly so a dead agent doesn't add a timeout to every pageview.
    """
    now = time.monotonic()

    if not force and now - _CACHE["checked_at"] < _CACHE_TTL_SECONDS:
        if _CACHE["url"]:
            return _CACHE["url"]
        raise TunnelUnavailable(_CACHE["error"] or "No tunnel detected.")

    last_error = None
    for candidate in agent_candidates(agent_api_url):
        try:
            url = _query_agent(candidate)
        except TunnelUnavailable as exc:
            last_error = exc
            continue
        _CACHE.update({"url": url, "checked_at": now, "error": ""})
        return url

    message = str(last_error) if last_error else "No tunnel detected."
    _CACHE.update({"url": None, "checked_at": now, "error": message})
    raise TunnelUnavailable(message)


def _query_agent(agent_api_url):
    import requests  # lazy: nothing here runs unless a tunnel is configured

    try:
        response = requests.get(agent_api_url, timeout=3)
    except Exception as exc:  # noqa: BLE001 -- any transport failure means "no agent"
        raise TunnelUnavailable(
            f"No ngrok agent at {agent_api_url} ({exc.__class__.__name__}). "
            "Start it with: docker compose --profile ngrok up -d"
        ) from exc

    if response.status_code >= 400:
        raise TunnelUnavailable(f"ngrok agent returned {response.status_code}.")

    try:
        tunnels = response.json().get("tunnels", [])
    except ValueError as exc:
        raise TunnelUnavailable("ngrok agent returned an unreadable response.") from exc

    for tunnel in tunnels:
        url = (tunnel.get("public_url") or "").strip()
        if url.startswith("https://"):
            return url.rstrip("/")

    # An http-only tunnel is not useful to AIDA, so say so explicitly.
    if tunnels:
        raise TunnelUnavailable(
            "The ngrok agent is running but published no HTTPS tunnel."
        )
    raise TunnelUnavailable("The ngrok agent is running but has no open tunnels.")


def clear_cache():
    _CACHE.update({"url": None, "checked_at": 0.0, "error": ""})


def effective_base_url(request=None):
    """The URL the rest of the app should advertise. Never raises."""
    from .models import TunnelSettings

    config = TunnelSettings.load()

    if config.mode == TunnelSettings.Mode.MANUAL and config.manual_url:
        return config.manual_url.rstrip("/")

    if config.mode == TunnelSettings.Mode.AUTO:
        try:
            url = detect_ngrok_url(config.agent_api_url)
        except TunnelUnavailable:
            pass
        else:
            # Remember it so the value survives a moment when the agent is down.
            config.remember(url)
            return url
        if config.detected_url:
            return config.detected_url.rstrip("/")

    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")

    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")

    return ""
