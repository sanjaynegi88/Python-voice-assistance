"""
Dashboard tests: every page renders for staff, and none of them leak to anonymous
visitors. Also pins the two things an operator relies on -- that the Settings
page prints the tool URLs, and that the Accounts page never prints a PIN.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from bank.models import (
    Account,
    Card,
    Customer,
    Notification,
    ProviderSettings,
    SecuritySettings,
    Ticket,
    ToolCallLog,
    TunnelSettings,
)
from bank import ngrok_agent
from bank.tunnel import (
    TunnelUnavailable,
    clear_cache as clear_tunnel_cache,
    detect_ngrok_url,
    is_local_request,
)

PAGES = [
    "dashboard:overview",
    "dashboard:customers",
    "dashboard:accounts",
    "dashboard:tickets",
    "dashboard:notifications",
    "dashboard:tool-logs",
    "dashboard:settings",
    "dashboard:providers",
    "dashboard:tunnel",
]


class DashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops", password="not-a-real-password", is_staff=True
        )
        cls.customer = Customer.objects.create(
            full_name="Priya Raman", phone="+41791112233", email="p@example.com"
        )
        cls.account = Account.objects.create(
            customer=cls.customer,
            account_number="1000000001",
            balance=Decimal("4250.75"),
        )
        cls.account.set_pin("4821")
        cls.account.save()
        cls.card = Card.objects.create(account=cls.account, last4="4417")
        cls.ticket = Ticket.objects.create(
            ticket_number="TKT-260904-K7QF2M",
            account=cls.account,
            card=cls.card,
            reason="stolen",
        )
        Notification.objects.create(
            account=cls.account,
            ticket=cls.ticket,
            channel="sms",
            destination="+41791112233",
            body="Dear Priya Raman, your card has been blocked.",
            status=Notification.Status.SENT,
        )
        ToolCallLog.objects.create(
            tool_name="get_account_balance",
            ok=True,
            account=cls.account,
            request_payload={"session_token": "***"},
            response_payload={"ok": True},
        )

    def test_anonymous_visitors_are_redirected_to_login(self):
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/dashboard/login/", response["Location"])

    def test_non_staff_users_cannot_get_in(self):
        get_user_model().objects.create_user(username="joe", password="x", is_staff=False)
        self.client.force_login(get_user_model().objects.get(username="joe"))
        response = self.client.get(reverse("dashboard:accounts"))
        self.assertEqual(response.status_code, 302)

    def test_every_page_renders_for_staff(self):
        self.client.force_login(self.staff)
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_customer_detail_renders(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("dashboard:customer-detail", args=[self.customer.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TKT-260904-K7QF2M")

    def test_accounts_page_shows_balances_but_never_a_pin(self):
        self.client.force_login(self.staff)
        body = self.client.get(reverse("dashboard:accounts")).content.decode()
        self.assertIn("4250.75", body)
        self.assertIn("1000000001", body)
        self.assertNotIn("4821", body)
        self.assertNotIn(self.account.pin_hash, body)

    @override_settings(
        TOOL_API_KEYS=["super-secret-key"], PUBLIC_BASE_URL="https://demo.example.com"
    )
    def test_settings_page_lists_the_tool_urls(self):
        self.client.force_login(self.staff)
        body = self.client.get(reverse("dashboard:settings")).content.decode()
        self.assertIn("https://demo.example.com/api/v1/tools/authenticate", body)
        self.assertIn("https://demo.example.com/api/v1/tools/get_balance", body)
        self.assertIn("https://demo.example.com/api/v1/tools/block_card", body)
        self.assertIn("https://demo.example.com/api/v1/tools/send_confirmation", body)
        # The key is present for the copy button but shown masked by default.
        self.assertIn("supe...-key", body)


class ProviderSettingsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops2", password="not-a-real-password", is_staff=True
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def test_page_renders_and_is_staff_only(self):
        self.assertEqual(self.client.get(reverse("dashboard:providers")).status_code, 200)
        self.client.logout()
        response = self.client.get(reverse("dashboard:providers"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/login/", response["Location"])

    def test_saving_brevo_credentials(self):
        response = self.client.post(
            reverse("dashboard:providers"),
            {
                "action": "save",
                "email_provider": "brevo",
                "sms_provider": "brevo",
                "default_channel": "sms",
                "brevo_api_key": "xkeysib-secret",
                "brevo_sender_email": "noreply@bank.example",
                "brevo_sender_name": "AIDA Bank",
                "brevo_sms_sender": "AIDABank",
                "smtp_port": 587,
                "smtp_use_tls": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        config = ProviderSettings.load()
        self.assertEqual(config.email_provider, "brevo")
        self.assertEqual(config.sms_provider, "brevo")
        self.assertEqual(config.brevo_api_key, "xkeysib-secret")
        self.assertEqual(config.updated_by, "ops2")
        self.assertTrue(config.is_ready("email"))
        self.assertTrue(config.is_ready("sms"))

    def test_stored_secret_is_never_rendered_back(self):
        config = ProviderSettings.load()
        config.brevo_api_key = "xkeysib-very-secret"
        config.twilio_auth_token = "twilio-very-secret"
        config.save()

        body = self.client.get(reverse("dashboard:providers")).content.decode()
        self.assertNotIn("xkeysib-very-secret", body)
        self.assertNotIn("twilio-very-secret", body)
        self.assertIn("leave blank to keep", body)

    def test_blank_secret_keeps_the_stored_value(self):
        config = ProviderSettings.load()
        config.brevo_api_key = "xkeysib-keep-me"
        config.save()

        self.client.post(
            reverse("dashboard:providers"),
            {
                "action": "save",
                "email_provider": "brevo",
                "sms_provider": "console",
                "default_channel": "sms",
                "brevo_api_key": "",  # untouched by the operator
                "brevo_sender_email": "noreply@bank.example",
                "brevo_sender_name": "AIDA Bank",
                "brevo_sms_sender": "AIDABank",
                "smtp_port": 587,
            },
        )
        self.assertEqual(ProviderSettings.load().brevo_api_key, "xkeysib-keep-me")

    def test_selecting_twilio_without_credentials_is_rejected(self):
        response = self.client.post(
            reverse("dashboard:providers"),
            {
                "action": "save",
                "email_provider": "console",
                "sms_provider": "twilio",
                "default_channel": "sms",
                "smtp_port": 587,
            },
        )
        self.assertEqual(response.status_code, 200)  # redisplayed with errors
        self.assertContains(response, "Twilio needs an account SID")
        self.assertEqual(ProviderSettings.load().sms_provider, "console")

    def test_test_message_reports_a_missing_configuration(self):
        config = ProviderSettings.load()
        config.sms_provider = "twilio"
        config.save()
        response = self.client.post(
            reverse("dashboard:providers"),
            {"action": "test", "test_channel": "sms", "test_destination": "+41791112233"},
            follow=True,
        )
        self.assertContains(response, "not fully configured")

    def test_console_test_message_succeeds(self):
        response = self.client.post(
            reverse("dashboard:providers"),
            {"action": "test", "test_channel": "sms", "test_destination": "+41791112233"},
            follow=True,
        )
        self.assertContains(response, "accepted by console")


class TunnelPageTests(TestCase):
    """The Live URL tab: detection, fallbacks, and the endpoint list it builds."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops3", password="not-a-real-password", is_staff=True
        )

    def setUp(self):
        self.client.force_login(self.staff)
        clear_tunnel_cache()

    def tearDown(self):
        clear_tunnel_cache()

    @override_settings(ALLOWED_HOSTS=["localhost", "testserver"])
    def test_page_renders_when_no_agent_is_running(self):
        with mock.patch(
            "bank.tunnel._query_agent", side_effect=TunnelUnavailable("No ngrok agent")
        ):
            response = self.client.get(
                reverse("dashboard:tunnel"), headers={"host": "localhost"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no tunnel")
        self.assertContains(response, "Start tunnel")

    def test_detected_url_drives_every_endpoint(self):
        with mock.patch(
            "bank.tunnel._query_agent", return_value="https://abc123.ngrok-free.app"
        ):
            response = self.client.get(reverse("dashboard:tunnel"))
        body = response.content.decode()
        self.assertContains(response, "https://abc123.ngrok-free.app")
        for path in ("authenticate", "get_balance", "block_card", "send_confirmation"):
            self.assertIn(f"https://abc123.ngrok-free.app/api/v1/tools/{path}", body)

    def test_detected_url_is_remembered(self):
        with mock.patch(
            "bank.tunnel._query_agent", return_value="https://abc123.ngrok-free.app"
        ):
            self.client.get(reverse("dashboard:tunnel"))
        config = TunnelSettings.load()
        self.assertEqual(config.detected_url, "https://abc123.ngrok-free.app")
        self.assertIsNotNone(config.detected_at)

    def test_integration_tab_uses_the_live_url_too(self):
        with mock.patch(
            "bank.tunnel._query_agent", return_value="https://abc123.ngrok-free.app"
        ):
            response = self.client.get(reverse("dashboard:settings"))
        self.assertContains(
            response, "https://abc123.ngrok-free.app/api/v1/tools/authenticate"
        )

    def test_manual_mode_wins_over_detection(self):
        config = TunnelSettings.load()
        config.mode = TunnelSettings.Mode.MANUAL
        config.manual_url = "https://fixed.example.com"
        config.save()
        with mock.patch(
            "bank.tunnel._query_agent", return_value="https://abc123.ngrok-free.app"
        ):
            response = self.client.get(reverse("dashboard:tunnel"))
        self.assertContains(response, "https://fixed.example.com/api/v1/tools/get_balance")
        self.assertNotContains(response, "abc123.ngrok-free.app")

    def test_saving_switches_mode(self):
        self.client.post(
            reverse("dashboard:tunnel"),
            {
                "action": "save",
                "mode": "manual",
                "manual_url": "https://fixed.example.com/",
                "agent_api_url": "http://ngrok:4040/api/tunnels",
            },
        )
        config = TunnelSettings.load()
        self.assertEqual(config.mode, "manual")
        # Trailing slash stripped so URLs never end up doubled.
        self.assertEqual(config.manual_url, "https://fixed.example.com")

    def test_refresh_reports_a_failure_without_breaking(self):
        with mock.patch(
            "bank.tunnel._query_agent", side_effect=TunnelUnavailable("agent is down")
        ):
            response = self.client.post(
                reverse("dashboard:tunnel"), {"action": "refresh"}, follow=True
            )
        self.assertContains(response, "agent is down")

    def test_http_only_tunnel_is_not_accepted(self):
        # requests is imported lazily inside _query_agent, so patch it at source.
        with mock.patch("requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {
                "tunnels": [{"public_url": "http://abc.ngrok-free.app"}]
            }
            with self.assertRaises(TunnelUnavailable):
                detect_ngrok_url("http://ngrok:4040/api/tunnels", force=True)

    def test_stale_url_is_used_while_the_agent_is_down(self):
        config = TunnelSettings.load()
        config.detected_url = "https://previous.ngrok-free.app"
        config.save()
        with mock.patch(
            "bank.tunnel._query_agent", side_effect=TunnelUnavailable("down")
        ):
            response = self.client.get(reverse("dashboard:tunnel"))
        self.assertContains(response, "https://previous.ngrok-free.app")


class LocalOnlyNgrokTests(TestCase):
    """
    The ngrok controls are for local development. Hiding the buttons is a
    courtesy; refusing the action server-side is the actual control.
    """

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops4", password="not-a-real-password", is_staff=True
        )

    def setUp(self):
        self.client.force_login(self.staff)
        clear_tunnel_cache()

    def tearDown(self):
        clear_tunnel_cache()

    def _get(self, host):
        with mock.patch(
            "bank.tunnel._query_agent", side_effect=TunnelUnavailable("no agent")
        ):
            return self.client.get(reverse("dashboard:tunnel"), headers={"host": host})

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_hostname_classification(self):
        factory = RequestFactory()
        for host, expected in [
            ("localhost:8000", True),
            ("127.0.0.1:8000", True),
            ("[::1]:8000", True),
            ("web:8000", True),
            ("app.localhost", True),
            ("192.168.1.20:8000", True),
            ("10.0.0.5", True),
            ("abc123.ngrok-free.app", False),
            ("bank.example.com", False),
            ("8.8.8.8", False),
        ]:
            with self.subTest(host=host):
                request = factory.get("/", headers={"host": host})
                self.assertEqual(is_local_request(request), expected)

    @override_settings(ALLOWED_HOSTS=["localhost", "bank.example.com"])
    def test_controls_appear_on_localhost(self):
        response = self._get("localhost")
        self.assertContains(response, "Start tunnel")
        self.assertContains(response, "Authtoken")

    @override_settings(ALLOWED_HOSTS=["localhost", "bank.example.com"])
    def test_controls_hidden_on_a_public_host(self):
        response = self._get("bank.example.com")
        self.assertNotContains(response, "Start tunnel")
        self.assertContains(response, "which isn't local")

    @override_settings(ALLOWED_HOSTS=["localhost", "bank.example.com"])
    def test_start_is_refused_from_a_public_host(self):
        with mock.patch("bank.ngrok_agent.start") as start:
            with mock.patch(
                "bank.tunnel._query_agent", side_effect=TunnelUnavailable("no agent")
            ):
                response = self.client.post(
                    reverse("dashboard:tunnel"),
                    {"action": "start"},
                    headers={"host": "bank.example.com"},
                    follow=True,
                )
        start.assert_not_called()
        self.assertContains(response, "only be controlled from a browser on the machine")

    @override_settings(ALLOWED_HOSTS=["localhost"])
    def test_start_saves_the_token_and_reports_the_url(self):
        with mock.patch(
            "bank.ngrok_agent.start", return_value="https://fresh.ngrok-free.app"
        ) as start:
            with mock.patch(
                "bank.tunnel._query_agent", side_effect=TunnelUnavailable("no agent")
            ):
                response = self.client.post(
                    reverse("dashboard:tunnel"),
                    {"action": "start", "ngrok_authtoken": "2abcDEF"},
                    headers={"host": "localhost"},
                    follow=True,
                )
        start.assert_called_once_with("2abcDEF")
        self.assertContains(response, "https://fresh.ngrok-free.app")
        config = TunnelSettings.load()
        self.assertEqual(config.ngrok_authtoken, "2abcDEF")
        self.assertEqual(config.detected_url, "https://fresh.ngrok-free.app")

    @override_settings(ALLOWED_HOSTS=["localhost"])
    def test_a_bad_authtoken_is_reported_not_swallowed(self):
        with mock.patch(
            "bank.ngrok_agent.start",
            side_effect=ngrok_agent.NgrokError("ERR_NGROK_105 authentication failed"),
        ):
            with mock.patch(
                "bank.tunnel._query_agent", side_effect=TunnelUnavailable("no agent")
            ):
                response = self.client.post(
                    reverse("dashboard:tunnel"),
                    {"action": "start", "ngrok_authtoken": "wrong"},
                    headers={"host": "localhost"},
                    follow=True,
                )
        self.assertContains(response, "ERR_NGROK_105")

    @override_settings(ALLOWED_HOSTS=["localhost"])
    def test_stored_authtoken_is_never_rendered_back(self):
        config = TunnelSettings.load()
        config.ngrok_authtoken = "2verySecretToken"
        config.save()
        response = self._get("localhost")
        self.assertNotContains(response, "2verySecretToken")
        self.assertContains(response, "leave blank to keep")

    @override_settings(ALLOWED_HOSTS=["localhost"])
    def test_blank_authtoken_keeps_the_stored_one(self):
        config = TunnelSettings.load()
        config.ngrok_authtoken = "2keepMe"
        config.save()
        with mock.patch("bank.ngrok_agent.start", return_value="https://x.ngrok-free.app"):
            with mock.patch(
                "bank.tunnel._query_agent", side_effect=TunnelUnavailable("no agent")
            ):
                self.client.post(
                    reverse("dashboard:tunnel"),
                    {"action": "start", "ngrok_authtoken": ""},
                    headers={"host": "localhost"},
                )
        self.assertEqual(TunnelSettings.load().ngrok_authtoken, "2keepMe")

    @override_settings(ALLOWED_HOSTS=["bank.example.com"])
    def test_controls_shown_when_browsing_through_a_live_tunnel(self):
        # Browsing via the tunnel is not "local", but an agent is clearly running,
        # so the stop control still has to be reachable.
        with mock.patch(
            "bank.tunnel._query_agent", return_value="https://abc.ngrok-free.app"
        ):
            response = self.client.get(
                reverse("dashboard:tunnel"), headers={"host": "bank.example.com"}
            )
        self.assertContains(response, "Stop tunnel")


class TestCredentialsPanelTests(TestCase):
    """
    The panel shows the seed PIN and whether it still authenticates. It must
    never claim a PIN works when it doesn't, and must say so plainly for an
    account that was never seeded.
    """

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops5", password="not-a-real-password", is_staff=True
        )
        cls.customer = Customer.objects.create(
            full_name="Sam Wilson", phone="+41793334455", email="sam@example.com"
        )
        cls.account = Account.objects.create(
            customer=cls.customer,
            account_number="5645342376",
            balance=Decimal("15780.25"),
        )
        cls.account.set_pin("7284")
        cls.account.save()

    def setUp(self):
        self.client.force_login(self.staff)

    def _get(self):
        return self.client.get(
            reverse("dashboard:customer-detail", args=[self.customer.pk])
        )

    def test_seed_pin_is_shown_and_verified(self):
        response = self._get()
        self.assertContains(response, "7284")
        self.assertContains(response, "verified against the stored hash")

    def test_a_changed_pin_is_reported_as_stale_not_as_working(self):
        self.account.set_pin("9999")
        self.account.save()
        response = self._get()
        self.assertContains(response, "stale")
        self.assertNotContains(response, "verified against the stored hash")

    def test_an_unseeded_account_says_so_rather_than_guessing(self):
        other = Customer.objects.create(full_name="Ada Byron", phone="+41790000000")
        account = Account.objects.create(
            customer=other, account_number="8888888888", balance=Decimal("1.00")
        )
        account.set_pin("1234")
        account.save()
        response = self.client.get(
            reverse("dashboard:customer-detail", args=[other.pk])
        )
        self.assertContains(response, "not seed data")
        self.assertNotContains(response, "1234")

    def test_national_id_is_labelled_as_not_an_auth_factor(self):
        self.assertContains(self._get(), "not used to authenticate")


class SecurityPolicyToggleTests(TestCase):
    """The one security setting an operator may change at runtime."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="ops6", password="not-a-real-password", is_staff=True
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def test_defaults_to_off(self):
        self.assertFalse(SecuritySettings.load().require_otp_for_card_block)

    def test_turning_it_on_and_off_records_who_did_it(self):
        self.client.post(
            reverse("dashboard:settings"),
            {"action": "security", "require_otp_for_card_block": "on"},
        )
        config = SecuritySettings.load()
        self.assertTrue(config.require_otp_for_card_block)
        self.assertEqual(config.updated_by, "ops6")

        # An unticked checkbox is simply absent from the POST.
        self.client.post(reverse("dashboard:settings"), {"action": "security"})
        self.assertFalse(SecuritySettings.load().require_otp_for_card_block)

    def test_the_toggle_changes_tool_behaviour_without_a_restart(self):
        customer = Customer.objects.create(full_name="Sam Wilson", phone="+41790000001")
        account = Account.objects.create(
            customer=customer, account_number="5645342376", balance=Decimal("10.00")
        )
        account.set_pin("7284")
        account.save()
        Card.objects.create(account=account, last4="8163")

        from bank import services

        session = services.authenticate(account_number="5645342376", pin="7284")

        # Off: the block goes straight through.
        self.assertFalse(SecuritySettings.load().require_otp_for_card_block)

        # On: the same session is now refused until it is elevated.
        self.client.post(
            reverse("dashboard:settings"),
            {"action": "security", "require_otp_for_card_block": "on"},
        )
        with self.assertRaises(services.ToolError) as caught:
            services.block_card(session, card_last4="8163", reason="lost")
        self.assertEqual(caught.exception.code, "OTP_REQUIRED")

    def test_health_endpoint_reports_the_live_value(self):
        self.client.post(
            reverse("dashboard:settings"),
            {"action": "security", "require_otp_for_card_block": "on"},
        )
        body = self.client.get("/api/v1/health").json()
        self.assertTrue(body["otp_required_for_card_block"])

    def test_page_warns_when_sms_cannot_actually_deliver(self):
        response = self.client.get(reverse("dashboard:settings"))
        self.assertContains(response, "check the SMS provider actually delivers")
