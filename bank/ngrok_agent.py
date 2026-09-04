"""
Start and stop the ngrok agent from the dashboard.

The agent runs as a child process of the web container rather than as a
separate compose service, which is what makes a Start button possible at all:
no Docker socket has to be mounted, and no privileged access is handed to the
web app. The trade-off is that this is a *development* convenience -- a web
process that can spawn other processes is not something you want on a public
host, so every entry point here is gated on the request coming from localhost
(see tunnel.is_local_request) and on DEBUG-style local use.

Gunicorn runs several workers, and a subprocess started by one of them is
invisible to the others. State is therefore kept on disk and in the agent
itself, not in Python memory:

  * the PID is written to a pidfile, so any worker can check or stop the agent
  * "is it up?" is answered by asking the agent's local API, which every worker
    in the container can reach
"""
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from django.conf import settings

from . import tunnel

logger = logging.getLogger("toolcall")


class NgrokError(Exception):
    """The agent could not be started, stopped, or found."""


def binary_path():
    """Absolute path to the ngrok binary, or None if it isn't installed."""
    configured = settings.NGROK_BINARY
    if configured and os.path.isabs(configured):
        return configured if os.path.exists(configured) else None
    return shutil.which(configured or "ngrok")


def _pidfile():
    return Path(settings.NGROK_PIDFILE)


def _logfile():
    return Path(settings.NGROK_LOGFILE)


def read_pid():
    try:
        return int(_pidfile().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def status():
    """
    What the agent is doing right now.

    Returns (running, url, detail). `running` is true when the agent answers on
    its local API -- that is the honest test, because the process can be alive
    while the tunnel is still being established or has been rejected.
    """
    pid = read_pid()
    alive = _process_alive(pid)

    try:
        url = tunnel.detect_ngrok_url(force=True)
        return True, url, f"Agent running (pid {pid})." if pid else "Agent running."
    except tunnel.TunnelUnavailable as exc:
        if alive:
            return True, None, f"Agent process is up (pid {pid}) but has no tunnel yet: {exc}"
        return False, None, str(exc)


def start(authtoken=None, *, forward_addr=None, wait_seconds=20):
    """
    Launch the agent and wait for it to publish an HTTPS tunnel.

    Returns the public URL. Raises NgrokError with something an operator can
    act on -- a bad authtoken is by far the most common failure, and ngrok
    reports it in its log rather than its exit code.
    """
    running, url, _ = status()
    if running and url:
        return url

    binary = binary_path()
    if not binary:
        raise NgrokError(
            "The ngrok binary isn't available in this container. Rebuild the "
            "image (docker compose up -d --build), or run the agent yourself "
            "with: docker compose --profile ngrok up -d"
        )

    authtoken = (authtoken or "").strip()
    if not authtoken:
        raise NgrokError(
            "No authtoken. Paste one from dashboard.ngrok.com and save before starting."
        )

    forward_addr = forward_addr or settings.NGROK_FORWARD_ADDR
    log_path = _logfile()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "NGROK_AUTHTOKEN": authtoken}
    command = [
        binary,
        "http",
        forward_addr,
        "--log", "stdout",
        "--log-format", "logfmt",
    ]

    logger.info("starting ngrok agent -> %s", forward_addr)
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives the worker that spawned it
            env=env,
        )

    _pidfile().write_text(str(process.pid), encoding="utf-8")

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _pidfile().unlink(missing_ok=True)
            raise NgrokError(f"The agent exited immediately. {recent_log(6)}")
        try:
            url = tunnel.detect_ngrok_url(force=True)
        except tunnel.TunnelUnavailable:
            time.sleep(0.5)
            continue
        logger.info("ngrok tunnel up: %s", url)
        return url

    stop()
    raise NgrokError(
        f"The agent didn't publish a tunnel within {wait_seconds}s. {recent_log(6)}"
    )


def stop():
    """Terminate the agent. Safe to call when it isn't running."""
    pid = read_pid()
    if not _process_alive(pid):
        _pidfile().unlink(missing_ok=True)
        tunnel.clear_cache()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise NgrokError(f"Could not stop the agent (pid {pid}): {exc}") from exc

    for _ in range(20):
        if not _process_alive(pid):
            break
        time.sleep(0.25)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    _pidfile().unlink(missing_ok=True)
    tunnel.clear_cache()
    logger.info("ngrok agent stopped (pid %s)", pid)
    return True


def recent_log(lines=12):
    """The tail of the agent's log -- where ngrok explains a bad authtoken."""
    try:
        text = _logfile().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no agent log available)"
    tail = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return " | ".join(tail) if tail else "(agent log is empty)"
