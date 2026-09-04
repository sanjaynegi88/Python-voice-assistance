"""
End-to-end tests for the tool API.

These double as the evidence for the evaluation checklist: authentication is
enforced server-side, wrong credentials are rejected gracefully, the three
seeded accounts return three different balances, and a card block writes a
ticket plus a notification.
"""
import json
from decimal import Decimal

from django.conf import settings
from django.test import Client, TestCase, override_settings

def enable_card_block_otp(test_method):
    """Turn on step-up for one test, through the same row the dashboard writes."""
    from functools import wraps

    @wraps(test_method)
    def wrapper(self, *args, **kwargs):
        from bank.models import SecuritySettings

        config = SecuritySettings.load()
        config.require_otp_for_card_block = True
        config.save()
        return test_method(self, *args, **kwargs)

    return wrapper


from bank.models import (
    Account,
    AuthSession,
    Card,
    Customer,
    Notification,
    Ticket,
    ToolCallLog,
)

API_KEY = "test-key"


def make_customer(name, phone, email, number, pin, balance, currency="USD", last4="1111"):
    customer = Customer.objects.create(full_name=name, phone=phone, email=email)
    account = Account.objects.create(
        customer=customer,
        account_number=number,
        balance=Decimal(balance),
        currency=currency,
    )
    account.set_pin(pin)
    account.save()
    Card.objects.create(account=account, last4=last4)
    return account


@override_settings(TOOL_API_KEYS=[API_KEY])
class ToolApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.priya = make_customer(
            "Priya Raman", "+41791112233", "p@example.com",
            "1000000001", "4821", "4250.75", last4="4417",
        )
        self.marcus = make_customer(
            "Marcus Bennet", "+41794445566", "m@example.com",
            "1000000002", "7391", "128.40", last4="6712",
        )
        self.elena = make_customer(
            "Elena Fischer", "+41797778899", "e@example.com",
            "1000000003", "5560", "92310.00", currency="CHF", last4="2285",
        )

    # -- helpers -----------------------------------------------------------
    def call(self, tool, payload, key=API_KEY):
        headers = {"X-API-Key": key} if key else {}
        return self.client.post(
            f"/api/v1/tools/{tool}",
            data=json.dumps(payload),
            content_type="application/json",
            headers=headers,
        )

    def login(self, account_number="1000000001", pin="4821"):
        response = self.call("authenticate", {"account_number": account_number, "pin": pin})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["session_token"]

    # -- transport ---------------------------------------------------------
    def test_missing_api_key_is_rejected(self):
        response = self.call("get_balance", {"session_token": "x"}, key=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED_CLIENT")

    def test_malformed_body_is_rejected_without_a_crash(self):
        response = self.client.post(
            "/api/v1/tools/authenticate",
            data="not json",
            content_type="application/json",
            headers={"X-API-Key": API_KEY},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "MALFORMED_REQUEST")

    # -- authentication ----------------------------------------------------
    def test_authentication_succeeds_and_returns_a_session(self):
        response = self.call(
            "authenticate", {"account_number": "1000000001", "pin": "4821"}
        )
        data = response.json()["data"]
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["customer_name"], "Priya Raman")
        self.assertEqual(data["account_number_masked"], "****0001")
        self.assertTrue(AuthSession.objects.filter(token=data["session_token"]).exists())

    def test_wrong_pin_is_rejected_gracefully(self):
        response = self.call(
            "authenticate", {"account_number": "1000000001", "pin": "0000"}
        )
        body = response.json()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INVALID_CREDENTIALS")
        self.assertIn("attempt", body["speech_hint"].lower())
        self.assertNotIn("session_token", json.dumps(body))

    def test_unknown_account_looks_the_same_as_a_wrong_pin(self):
        response = self.call(
            "authenticate", {"account_number": "9999999999", "pin": "4821"}
        )
        self.assertEqual(response.json()["error"]["code"], "INVALID_CREDENTIALS")

    def test_account_locks_after_repeated_failures(self):
        for _ in range(settings.MAX_PIN_ATTEMPTS):
            self.call("authenticate", {"account_number": "1000000001", "pin": "0000"})

        response = self.call(
            "authenticate", {"account_number": "1000000001", "pin": "4821"}
        )
        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_LOCKED")

    def test_frozen_account_cannot_authenticate(self):
        self.marcus.status = Account.Status.FROZEN
        self.marcus.save()
        response = self.call(
            "authenticate", {"account_number": "1000000002", "pin": "7391"}
        )
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_INACTIVE")

    # -- balance -----------------------------------------------------------
    def test_balance_requires_a_session(self):
        response = self.call("get_balance", {})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "SESSION_REQUIRED")

    def test_forged_session_token_is_rejected(self):
        response = self.call("get_balance", {"session_token": "totally-made-up"})
        self.assertEqual(response.json()["error"]["code"], "SESSION_INVALID")

    def test_each_account_returns_its_own_balance(self):
        expected = {
            ("1000000001", "4821"): ("4250.75", "USD"),
            ("1000000002", "7391"): ("128.40", "USD"),
            ("1000000003", "5560"): ("92310.00", "CHF"),
        }
        seen = set()
        for (number, pin), (balance, currency) in expected.items():
            token = self.login(number, pin)
            data = self.call("get_balance", {"session_token": token}).json()["data"]
            self.assertEqual(data["balance"], balance)
            self.assertEqual(data["currency"], currency)
            seen.add(data["balance"])
        self.assertEqual(len(seen), 3, "balances must be distinct across accounts")

    def test_balance_is_spoken_in_words(self):
        token = self.login()
        data = self.call("get_balance", {"session_token": token}).json()["data"]
        self.assertEqual(
            data["balance_spoken"],
            "four thousand two hundred and fifty dollars and seventy-five cents",
        )

    def test_one_session_cannot_read_another_account(self):
        token = self.login("1000000002", "7391")
        data = self.call("get_balance", {"session_token": token}).json()["data"]
        self.assertEqual(data["balance"], "128.40")
        self.assertEqual(data["customer_name"], "Marcus Bennet")

    # -- cards and tickets -------------------------------------------------
    def test_list_cards_names_the_active_card(self):
        token = self.login()
        data = self.call("list_cards", {"session_token": token}).json()["data"]
        self.assertEqual(len(data["cards"]), 1)
        self.assertEqual(data["cards"][0]["last4"], "4417")

    def test_block_card_creates_a_ticket_and_blocks_the_card(self):
        token = self.login()
        response = self.call(
            "block_card",
            {
                "session_token": token,
                "card_last4": "4417",
                "reason": "stolen",
                "incident_description": "Wallet taken on the tram.",
            },
        )
        data = response.json()["data"]
        self.assertTrue(data["blocked"])
        self.assertTrue(data["ticket_number"].startswith("TKT-"))

        card = Card.objects.get(account=self.priya, last4="4417")
        self.assertEqual(card.status, Card.Status.BLOCKED)

        ticket = Ticket.objects.get(ticket_number=data["ticket_number"])
        self.assertEqual(ticket.account, self.priya)
        self.assertEqual(ticket.reason, "stolen")
        self.assertEqual(ticket.card, card)

    def test_block_card_requires_authentication(self):
        response = self.call("block_card", {"card_last4": "4417", "reason": "lost"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Card.objects.filter(status=Card.Status.BLOCKED).count(), 0)

    def test_blocking_an_unknown_card_is_refused(self):
        token = self.login()
        response = self.call(
            "block_card",
            {"session_token": token, "card_last4": "0000", "reason": "lost"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "CARD_NOT_FOUND")

    def test_blocking_twice_is_refused(self):
        token = self.login()
        payload = {"session_token": token, "card_last4": "4417", "reason": "lost"}
        self.call("block_card", payload)
        response = self.call("block_card", payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CARD_ALREADY_BLOCKED")
        self.assertEqual(Ticket.objects.count(), 1)

    def test_a_caller_cannot_block_someone_elses_card(self):
        token = self.login("1000000002", "7391")
        response = self.call(
            "block_card",
            {"session_token": token, "card_last4": "4417", "reason": "lost"},
        )
        self.assertEqual(response.json()["error"]["code"], "CARD_NOT_FOUND")
        self.assertEqual(
            Card.objects.get(account=self.priya, last4="4417").status,
            Card.Status.ACTIVE,
        )

    # -- confirmation ------------------------------------------------------
    def test_confirmation_contains_name_ticket_and_outcome(self):
        token = self.login()
        ticket_number = self.call(
            "block_card",
            {"session_token": token, "card_last4": "4417", "reason": "stolen"},
        ).json()["data"]["ticket_number"]

        response = self.call(
            "send_confirmation",
            {"session_token": token, "ticket_number": ticket_number, "channel": "sms"},
        )
        self.assertTrue(response.json()["data"]["sent"])

        notification = Notification.objects.get(ticket__ticket_number=ticket_number)
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertEqual(notification.destination, "+41791112233")
        self.assertIn("Priya Raman", notification.body)
        self.assertIn(ticket_number, notification.body)
        self.assertIn("blocked", notification.body.lower())

    def test_confirmation_for_another_customers_ticket_is_refused(self):
        priya_token = self.login()
        ticket_number = self.call(
            "block_card",
            {"session_token": priya_token, "card_last4": "4417", "reason": "lost"},
        ).json()["data"]["ticket_number"]

        marcus_token = self.login("1000000002", "7391")
        response = self.call(
            "send_confirmation",
            {"session_token": marcus_token, "ticket_number": ticket_number},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "TICKET_NOT_FOUND")

    # -- step-up -----------------------------------------------------------
    @enable_card_block_otp
    def test_otp_step_up_gates_the_card_block(self):
        token = self.login()
        blocked = self.call(
            "block_card",
            {"session_token": token, "card_last4": "4417", "reason": "lost"},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"]["code"], "OTP_REQUIRED")

        self.assertTrue(self.call("send_otp", {"session_token": token}).json()["ok"])

        wrong = self.call("verify_otp", {"session_token": token, "code": "000000"})
        self.assertEqual(wrong.json()["error"]["code"], "OTP_INVALID")

        # Recover the real code from the message the console provider stored.
        body = Notification.objects.filter(ticket__isnull=True).latest("created_at").body
        code = "".join(ch for ch in body.split("code is")[1][:12] if ch.isdigit())

        self.assertTrue(
            self.call("verify_otp", {"session_token": token, "code": code}).json()["ok"]
        )
        after = self.call(
            "block_card",
            {"session_token": token, "card_last4": "4417", "reason": "lost"},
        )
        self.assertTrue(after.json()["data"]["blocked"])

    # -- auditing ----------------------------------------------------------
    def test_every_call_is_logged_with_secrets_redacted(self):
        token = self.login()
        self.call("get_balance", {"session_token": token})

        auth_log = ToolCallLog.objects.get(tool_name="authenticate_caller")
        self.assertEqual(auth_log.request_payload["pin"], "***")
        self.assertEqual(auth_log.response_payload["data"]["session_token"], "***")
        self.assertTrue(auth_log.ok)
        self.assertEqual(auth_log.account, self.priya)

        balance_log = ToolCallLog.objects.get(tool_name="get_account_balance")
        self.assertEqual(balance_log.request_payload["session_token"], "***")
        self.assertEqual(balance_log.account, self.priya)

    def test_failed_calls_are_logged_too(self):
        self.call("authenticate", {"account_number": "1000000001", "pin": "0000"})
        log = ToolCallLog.objects.get(tool_name="authenticate_caller")
        self.assertFalse(log.ok)
        self.assertEqual(log.error_code, "INVALID_CREDENTIALS")
        self.assertEqual(log.http_status, 401)


@override_settings(TOOL_API_KEYS=[API_KEY])
class MetaEndpointTests(TestCase):
    def test_health_needs_no_key(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_schema_needs_a_key(self):
        self.assertEqual(self.client.get("/api/v1/tools/schema").status_code, 401)
        response = self.client.get(
            "/api/v1/tools/schema", headers={"X-API-Key": API_KEY}
        )
        names = [tool["function"]["name"] for tool in response.json()["tools"]]
        self.assertIn("authenticate_caller", names)
        self.assertIn("get_account_balance", names)
        self.assertIn("block_card", names)
        self.assertIn("send_confirmation", names)


class ApiKeyTransportTests(TestCase):
    """
    The key must be presentable four ways, because low-code platforms differ in
    what they allow per tool. AIDA's tool definition has no headers field, so
    the query-string route is the one that works there.
    """

    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(
            full_name="Priya Raman", phone="+41791112233", email="p@example.com"
        )
        cls.account = Account.objects.create(
            customer=cls.customer, account_number="1000000001", balance=Decimal("10.00")
        )
        cls.account.set_pin("4821")
        cls.account.save()

    def _post(self, **extra):
        body = extra.pop("body", {"account_number": "1000000001", "pin": "4821"})
        return self.client.post(
            "/api/v1/tools/authenticate" + extra.pop("query", ""),
            data=json.dumps(body),
            content_type="application/json",
            **extra,
        )

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_header(self):
        response = self._post(headers={"x-api-key": "k-secret"})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_bearer_token(self):
        response = self._post(headers={"authorization": "Bearer k-secret"})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_query_parameter(self):
        response = self._post(query="?api_key=k-secret")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["authenticated"])

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_body_field_and_the_body_still_parses(self):
        response = self._post(
            body={"account_number": "1000000001", "pin": "4821", "api_key": "k-secret"}
        )
        self.assertEqual(response.status_code, 200)
        # Reading the key out of the body must not consume it for the handler.
        self.assertEqual(response.json()["data"]["customer_name"], "Priya Raman")

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_wrong_key_in_any_position_is_rejected(self):
        for label, kwargs in [
            ("header", {"headers": {"x-api-key": "nope"}}),
            ("query", {"query": "?api_key=nope"}),
            ("body", {"body": {"account_number": "1", "pin": "2", "api_key": "nope"}}),
        ]:
            with self.subTest(position=label):
                self.assertEqual(self._post(**kwargs).status_code, 401)

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_rejection_is_recorded_for_the_dashboard(self):
        ToolCallLog.objects.all().delete()
        self._post()  # no key at all
        log = ToolCallLog.objects.get()
        self.assertEqual(log.error_code, "UNAUTHORIZED_CLIENT")
        self.assertEqual(log.http_status, 401)
        self.assertFalse(log.ok)
        # The PIN in the rejected body must still be redacted.
        self.assertEqual(log.request_payload["pin"], "***")

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_rejection_message_names_the_ways_to_send_it(self):
        message = self._post().json()["error"]["message"]
        for hint in ("X-API-Key", "Bearer", "api_key="):
            self.assertIn(hint, message)

    @override_settings(TOOL_API_KEYS=["k-secret"])
    def test_key_is_never_echoed_into_the_log(self):
        ToolCallLog.objects.all().delete()
        self._post(
            body={"account_number": "1000000001", "pin": "4821", "api_key": "k-secret"}
        )
        log = ToolCallLog.objects.get()
        self.assertEqual(log.request_payload["api_key"], "***")
        self.assertNotIn("k-secret", json.dumps(log.request_payload))


class RedactionScopeTests(TestCase):
    """
    "code" is an OTP in a request and an error code in a response. Redacting it
    in responses hid the one field you need when a call fails.
    """

    @override_settings(TOOL_API_KEYS=["k"])
    def test_error_code_survives_in_the_response_log(self):
        ToolCallLog.objects.all().delete()
        self.client.post(
            "/api/v1/tools/authenticate",
            data=json.dumps({"account_number": "999", "pin": "0000"}),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )
        log = ToolCallLog.objects.get()
        self.assertEqual(log.response_payload["error"]["code"], "INVALID_CREDENTIALS")
        self.assertEqual(log.request_payload["pin"], "***")

    @override_settings(TOOL_API_KEYS=["k"])
    def test_otp_code_is_still_redacted_in_requests(self):
        ToolCallLog.objects.all().delete()
        self.client.post(
            "/api/v1/tools/verify_otp",
            data=json.dumps({"session_token": "t", "code": "123456"}),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )
        log = ToolCallLog.objects.get()
        self.assertEqual(log.request_payload["code"], "***")
        self.assertNotIn("123456", json.dumps(log.request_payload))


class NumericJsonScalarTests(TestCase):
    """
    Platforms and LLM tool-calling routinely send numeric-looking fields as JSON
    numbers regardless of the declared schema. AIDA did exactly this and crashed
    authenticate_caller with 'int' object has no attribute 'strip'.
    """

    @classmethod
    def setUpTestData(cls):
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

    def _auth(self, body):
        return self.client.post(
            "/api/v1/tools/authenticate",
            data=json.dumps(body),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )

    @override_settings(TOOL_API_KEYS=["k"])
    def test_pin_and_account_number_as_json_numbers(self):
        response = self._auth({"account_number": 5645342376, "pin": 7284})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["authenticated"])

    @override_settings(TOOL_API_KEYS=["k"])
    def test_integral_floats_do_not_become_7284_point_0(self):
        response = self._auth({"account_number": 5645342376.0, "pin": 7284.0})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k"])
    def test_strings_still_work(self):
        response = self._auth({"account_number": "5645342376", "pin": "7284"})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k"])
    def test_a_wrong_numeric_pin_is_still_a_clean_401_not_a_500(self):
        response = self._auth({"account_number": 5645342376, "pin": 1111})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_CREDENTIALS")

    @override_settings(TOOL_API_KEYS=["k"])
    def test_leading_zero_pin_survives_numeric_transport(self):
        self.account.set_pin("0421")
        self.account.save()
        # A platform that retypes "0421" as a number sends 421.
        response = self._auth({"account_number": 5645342376, "pin": 421})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k"])
    def test_no_tool_returns_500_for_a_numeric_session_token(self):
        response = self.client.post(
            "/api/v1/tools/get_balance",
            data=json.dumps({"session_token": 12345}),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )
        self.assertEqual(response.status_code, 401)  # invalid, not crashed
        self.assertNotEqual(response.json()["error"]["code"], "INTERNAL_ERROR")


class SessionTokenFieldNameTests(TestCase):
    """
    AIDA returns the token from authenticate_caller under the name "sessionID".
    Reading only "session_token" produced SESSION_REQUIRED on every downstream
    call, whose speech hint asks the caller to verify again -- an infinite
    "what is your account number" loop on a live phone call.
    """

    @classmethod
    def setUpTestData(cls):
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

    def _token(self):
        response = self.client.post(
            "/api/v1/tools/authenticate",
            data=json.dumps({"account_number": "5645342376", "pin": "7284"}),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )
        return response.json()["data"]["session_token"]

    def _balance(self, body):
        return self.client.post(
            "/api/v1/tools/get_balance",
            data=json.dumps(body),
            content_type="application/json",
            headers={"x-api-key": "k"},
        )

    @override_settings(TOOL_API_KEYS=["k"])
    def test_every_accepted_spelling_works(self):
        token = self._token()
        for field in (
            "session_token",
            "sessionToken",
            "sessionID",
            "sessionId",
            "session_id",
            "token",
        ):
            with self.subTest(field=field):
                response = self._balance({field: token})
                self.assertEqual(response.status_code, 200, field)
                self.assertEqual(response.json()["data"]["balance"], "15780.25")

    @override_settings(TOOL_API_KEYS=["k"])
    def test_field_name_matching_is_case_insensitive(self):
        response = self._balance({"SessionID": self._token()})
        self.assertEqual(response.status_code, 200)

    @override_settings(TOOL_API_KEYS=["k"])
    def test_a_genuinely_missing_token_still_fails(self):
        response = self._balance({})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "SESSION_REQUIRED")

    @override_settings(TOOL_API_KEYS=["k"])
    def test_a_bogus_token_under_an_alias_is_still_rejected(self):
        response = self._balance({"sessionID": "not-a-real-token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "SESSION_INVALID")

    @override_settings(TOOL_API_KEYS=["k"])
    def test_the_token_is_redacted_under_every_alias(self):
        token = self._token()
        ToolCallLog.objects.all().delete()
        self._balance({"sessionID": token})
        log = ToolCallLog.objects.get()
        self.assertEqual(log.request_payload["sessionID"], "***")
        self.assertNotIn(token, json.dumps(log.request_payload))


class OtpChannelWordingTests(TestCase):
    """The step-up prompt must name the channel the code actually arrives on."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(
            full_name="Sam Wilson", phone="+41793334455", email="sam@example.com"
        )
        cls.account = Account.objects.create(
            customer=cls.customer, account_number="5645342376", balance=Decimal("10.00")
        )
        cls.account.set_pin("7284")
        cls.account.save()
        Card.objects.create(account=cls.account, last4="8163")

    def _gated_hint(self, channel):
        from bank.models import ProviderSettings, SecuritySettings
        from bank import services

        config = ProviderSettings.load()
        config.default_channel = channel
        config.save()
        security = SecuritySettings.load()
        security.require_otp_for_card_block = True
        security.save()

        session = services.authenticate(account_number="5645342376", pin="7284")
        with self.assertRaises(services.ToolError) as caught:
            services.block_card(session, card_last4="8163", reason="lost")
        return caught.exception.speech_hint

    def test_email_channel_says_email(self):
        hint = self._gated_hint("email")
        self.assertIn("email address on your account", hint)
        self.assertNotIn("number on your account", hint)

    def test_sms_channel_says_number(self):
        hint = self._gated_hint("sms")
        self.assertIn("number on your account", hint)
