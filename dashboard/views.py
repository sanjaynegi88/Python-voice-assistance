"""
Staff-only operations dashboard.

Read-mostly: it exists so a human can see who the customers are, what their
accounts hold, and exactly which tool calls the voice agent made -- plus a
Settings page that hands you the URLs and key to paste into AIDA.
"""
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from api.schema import endpoints, function_definitions
from dashboard.forms import ProviderSettingsForm
from bank.notifications import NotificationError, send_test_message
from bank import ngrok_agent
from bank.demo_data import seed_pin_for
from bank.tunnel import (
    TunnelUnavailable,
    clear_cache as clear_tunnel_cache,
    detect_ngrok_url,
    effective_base_url,
    is_local_request,
)
from bank.models import (
    Account,
    AuthSession,
    Card,
    Customer,
    Notification,
    ProviderSettings,
    SecuritySettings,
    Ticket,
    ToolCallLog,
    TunnelSettings,
)

import json

PROMPT_PATH = Path(django_settings.BASE_DIR) / "docs" / "aida-agent-prompt.md"


def staff_required(view_func):
    """staff_member_required, but bounced to our own login page not Django admin's."""
    return staff_member_required(view_func, login_url="dashboard:login")


def _nav(active):
    return {"active": active}


@staff_required
def overview(request):
    since = timezone.now() - timedelta(hours=24)
    calls = ToolCallLog.objects.filter(created_at__gte=since)

    per_tool = (
        calls.values("tool_name")
        .annotate(
            total=Count("id"),
            failures=Count("id", filter=Q(ok=False)),
        )
        .order_by("-total")
    )

    totals_by_currency = (
        Account.objects.values("currency")
        .annotate(total=Sum("balance"), accounts=Count("id"))
        .order_by("currency")
    )

    context = {
        **_nav("overview"),
        "customer_count": Customer.objects.count(),
        "account_count": Account.objects.count(),
        "card_count": Card.objects.count(),
        "blocked_card_count": Card.objects.filter(status=Card.Status.BLOCKED).count(),
        "open_ticket_count": Ticket.objects.filter(status=Ticket.Status.OPEN).count(),
        "active_session_count": AuthSession.objects.filter(
            revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).count(),
        "calls_24h": calls.count(),
        "failures_24h": calls.filter(ok=False).count(),
        "per_tool": per_tool,
        "totals_by_currency": totals_by_currency,
        "recent_calls": ToolCallLog.objects.select_related("account")[:12],
        "recent_tickets": Ticket.objects.select_related("account", "card")[:6],
    }
    return render(request, "dashboard/overview.html", context)


@staff_required
def customers(request):
    query = request.GET.get("q", "").strip()
    rows = Customer.objects.prefetch_related("accounts", "accounts__cards")
    if query:
        rows = rows.filter(
            Q(full_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
            | Q(accounts__account_number__icontains=query)
        ).distinct()
    return render(
        request,
        "dashboard/customers.html",
        {**_nav("customers"), "customers": rows, "query": query},
    )


@staff_required
def customer_detail(request, pk):
    customer = get_object_or_404(
        Customer.objects.prefetch_related("accounts", "accounts__cards"), pk=pk
    )
    account_ids = list(customer.accounts.values_list("id", flat=True))

    # PINs are hashed and cannot be read back, so show the seed PIN from
    # demo_data and verify it against the stored hash. That way the page tells
    # you what to type *and* whether it still works -- a PIN changed in the
    # admin shows up here as stale rather than silently misleading you.
    test_credentials = []
    for account in customer.accounts.all():
        pin = seed_pin_for(account.account_number)
        test_credentials.append(
            {
                "account": account,
                "pin": pin,
                "is_seeded": bool(pin),
                "still_valid": bool(pin) and account.check_pin(pin),
            }
        )

    return render(
        request,
        "dashboard/customer_detail.html",
        {
            **_nav("customers"),
            "customer": customer,
            "test_credentials": test_credentials,
            "tickets": Ticket.objects.filter(account_id__in=account_ids)[:20],
            "notifications": Notification.objects.filter(account_id__in=account_ids)[:20],
            "calls": ToolCallLog.objects.filter(account_id__in=account_ids)[:20],
        },
    )


@staff_required
def accounts(request):
    rows = Account.objects.select_related("customer").prefetch_related("cards")
    return render(
        request,
        "dashboard/accounts.html",
        {
            **_nav("accounts"),
            "accounts": rows,
            "grand_totals": rows.values("currency").annotate(total=Sum("balance")),
        },
    )


@staff_required
def tickets(request):
    return render(
        request,
        "dashboard/tickets.html",
        {
            **_nav("tickets"),
            "tickets": Ticket.objects.select_related(
                "account", "account__customer", "card"
            ).prefetch_related("notifications")[:200],
        },
    )


@staff_required
def notifications(request):
    return render(
        request,
        "dashboard/notifications.html",
        {
            **_nav("notifications"),
            "notifications": Notification.objects.select_related(
                "account", "account__customer", "ticket"
            )[:200],
        },
    )


@staff_required
def tool_logs(request):
    tool = request.GET.get("tool", "").strip()
    only_failures = request.GET.get("failures") == "1"
    rows = ToolCallLog.objects.select_related("account", "account__customer")
    if tool:
        rows = rows.filter(tool_name=tool)
    if only_failures:
        rows = rows.filter(ok=False)

    return render(
        request,
        "dashboard/tool_logs.html",
        {
            **_nav("logs"),
            "logs": rows[:200],
            "tool_names": ToolCallLog.objects.values_list(
                "tool_name", flat=True
            ).distinct(),
            "selected_tool": tool,
            "only_failures": only_failures,
        },
    )


@staff_required
def settings_view(request):
    """Everything you need to wire this backend into AIDA, on one page."""
    security = SecuritySettings.load()

    if request.method == "POST" and request.POST.get("action") == "security":
        # The checkbox is absent from the POST when unticked.
        security.require_otp_for_card_block = (
            request.POST.get("require_otp_for_card_block") == "on"
        )
        security.updated_by = request.user.get_username()
        security.save()
        state = "on" if security.require_otp_for_card_block else "off"
        messages.success(
            request, f"Step-up verification for card blocks is now {state}."
        )
        return redirect("dashboard:settings")

    base_url = effective_base_url(request)
    keys = django_settings.TOOL_API_KEYS
    primary_key = keys[0] if keys else ""

    prompt_text = ""
    if PROMPT_PATH.exists():
        prompt_text = PROMPT_PATH.read_text(encoding="utf-8")

    curl_example = (
        f"curl -X POST {base_url}/api/v1/tools/authenticate \\\n"
        f'  -H "Content-Type: application/json" \\\n'
        f'  -H "X-API-Key: {primary_key}" \\\n'
        f"  -d '{{\"account_number\": \"1000000001\", \"pin\": \"4821\"}}'"
    )

    # Pre-serialise the JSON blobs: Django templates have no json filter.
    endpoint_rows = []
    for tool in endpoints(base_url):
        endpoint_rows.append(
            {
                **tool,
                "parameters_json": json.dumps(tool["parameters"], indent=2),
                "sample_request_json": json.dumps(tool["sample_request"], indent=2),
                "sample_response_json": json.dumps(tool["sample_response"], indent=2),
                "required_params": tool["parameters"].get("required", []),
            }
        )

    provider_config = ProviderSettings.load()

    runtime = [
        ("Email provider", provider_config.get_email_provider_display()),
        ("SMS provider", provider_config.get_sms_provider_display()),
        ("Default channel", provider_config.default_channel),
        ("Session TTL", f"{django_settings.AUTH_SESSION_TTL_SECONDS} s"),
        ("Max PIN attempts", django_settings.MAX_PIN_ATTEMPTS),
        ("Lockout duration", f"{django_settings.LOCKOUT_MINUTES} min"),
        ("OTP TTL", f"{django_settings.OTP_TTL_SECONDS} s"),
        ("Database engine", django_settings.DATABASES["default"]["ENGINE"].split(".")[-1]),
        ("Debug mode", django_settings.DEBUG),
    ]

    return render(
        request,
        "dashboard/settings.html",
        {
            **_nav("settings"),
            "base_url": base_url,
            "public_base_url_configured": bool(
                django_settings.PUBLIC_BASE_URL
                or TunnelSettings.load().effective_hint
            ),
            "api_key": primary_key,
            "api_key_masked": (primary_key[:4] + "..." + primary_key[-4:])
            if len(primary_key) > 10
            else "***",
            "key_count": len(keys),
            "endpoints": endpoint_rows,
            "schema_json": json.dumps(function_definitions(), indent=2),
            "curl_example": curl_example,
            "runtime": runtime,
            "provider_config": provider_config,
            "security": security,
            "prompt_text": prompt_text,
            "prompt_path": str(PROMPT_PATH),
        },
    )


@staff_required
def providers_view(request):
    """
    Edit the email/SMS provider credentials, and send a test message.

    Three POST actions, distinguished by the submit button's name:
      save    -- validate and persist the form
      test    -- send a one-off message to an address you type in
      reload  -- discard the stored row and re-seed it from the environment
    """
    config = ProviderSettings.load()

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "reload":
            for field, value in ProviderSettings.defaults_from_env().items():
                setattr(config, field, value)
            config.updated_by = request.user.get_username()
            config.save()
            messages.success(request, "Reloaded provider settings from the environment.")
            return redirect("dashboard:providers")

        if action == "test":
            channel = request.POST.get("test_channel") or config.default_channel
            destination = (request.POST.get("test_destination") or "").strip()
            try:
                message_id = send_test_message(
                    channel=channel, destination=destination, config=config
                )
                messages.success(
                    request,
                    f"Test {channel} accepted by "
                    f"{config.provider_for(channel)} (id {message_id or 'n/a'}).",
                )
            except NotificationError as exc:
                messages.error(request, f"Test {channel} failed: {exc}")
            return redirect("dashboard:providers")

        form = ProviderSettingsForm(request.POST, instance=config)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user.get_username()
            saved.save()
            messages.success(request, "Provider settings saved.")
            return redirect("dashboard:providers")
        messages.error(request, "Nothing was saved — please fix the errors below.")
    else:
        form = ProviderSettingsForm(instance=config)

    return render(
        request,
        "dashboard/providers.html",
        {
            **_nav("settings"),
            "form": form,
            "config": config,
            "email_ready": config.is_ready("email"),
            "sms_ready": config.is_ready("sms"),
            "email_missing": config.missing_fields_for("email"),
            "sms_missing": config.missing_fields_for("sms"),
            "recent": Notification.objects.all()[:10],
        },
    )


@staff_required
def tunnel_view(request):
    """
    The live public URL, every tool endpoint rebuilt on it, and -- when the
    dashboard is being browsed locally -- controls to run the ngrok agent.

    The agent is a child process of this container. Spawning processes from a
    web request is a development convenience and nothing more, so the start and
    stop actions are refused unless the request came from this machine. Hiding
    the buttons is not the control; this check is.
    """
    config = TunnelSettings.load()
    local = is_local_request(request)
    agent_running, agent_url, agent_detail = ngrok_agent.status()
    # Worth offering the controls if you're local, or if an agent is already up
    # (which is the case when you're browsing through the tunnel itself).
    show_ngrok = local or agent_running

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action in {"start", "stop"} and not local:
            messages.error(
                request,
                "The ngrok agent can only be controlled from a browser on the "
                "machine running this service.",
            )
            return redirect("dashboard:tunnel")

        if action == "start":
            token = (request.POST.get("ngrok_authtoken") or "").strip()
            if token:
                config.ngrok_authtoken = token
                config.save(update_fields=["ngrok_authtoken", "updated_at"])
            try:
                url = ngrok_agent.start(config.ngrok_authtoken)
            except ngrok_agent.NgrokError as exc:
                messages.error(request, str(exc))
            else:
                config.mode = TunnelSettings.Mode.AUTO
                config.save(update_fields=["mode", "updated_at"])
                config.remember(url)
                messages.success(request, f"Tunnel is live at {url}")
            return redirect("dashboard:tunnel")

        if action == "stop":
            try:
                stopped = ngrok_agent.stop()
            except ngrok_agent.NgrokError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Tunnel stopped." if stopped else "No agent was running.",
                )
            return redirect("dashboard:tunnel")

        if action == "refresh":
            try:
                url = detect_ngrok_url(config.agent_api_url, force=True)
                config.remember(url)
                messages.success(request, f"Detected tunnel: {url}")
            except TunnelUnavailable as exc:
                messages.error(request, str(exc))
            return redirect("dashboard:tunnel")

        config.mode = request.POST.get("mode", config.mode)
        config.manual_url = (request.POST.get("manual_url") or "").strip().rstrip("/")
        config.agent_api_url = (
            request.POST.get("agent_api_url") or django_settings.NGROK_API_URL
        ).strip()
        token = (request.POST.get("ngrok_authtoken") or "").strip()
        if token:  # blank means "leave the stored one alone"
            config.ngrok_authtoken = token
        config.save()
        clear_tunnel_cache()
        messages.success(request, "Tunnel settings saved.")
        return redirect("dashboard:tunnel")

    detected, detect_error = None, ""
    if config.mode == TunnelSettings.Mode.AUTO:
        if agent_url:
            detected = agent_url
            config.remember(detected)
        else:
            detect_error = agent_detail

    base_url = effective_base_url(request)
    live = bool(detected) or config.mode != TunnelSettings.Mode.AUTO

    return render(
        request,
        "dashboard/tunnel.html",
        {
            **_nav("settings"),
            "config": config,
            "modes": TunnelSettings.Mode.choices,
            "detected": detected,
            "detect_error": detect_error,
            "base_url": base_url,
            "live": live,
            "endpoints": endpoints(base_url),
            "endpoints_keyed": [
                {**e, "url": f"{e['url']}?api_key={(django_settings.TOOL_API_KEYS or [''])[0]}"}
                for e in endpoints(base_url)
            ],
            "health_url": f"{base_url}/api/v1/health" if base_url else "",
            "api_key": (django_settings.TOOL_API_KEYS or [""])[0],
            "env_base_url": django_settings.PUBLIC_BASE_URL,
            "is_local": local,
            "show_ngrok": show_ngrok,
            "agent_running": agent_running,
            "agent_detail": agent_detail,
            "agent_available": bool(ngrok_agent.binary_path()),
            "has_authtoken": bool(config.ngrok_authtoken),
            "browsing_host": request.get_host(),
        },
    )
