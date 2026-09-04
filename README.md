# AIDA Voice Bank — agentic tool backend

Backend for a voice bot on the **Enterprise Bot AIDA** platform. A caller phones
in, authenticates, asks for their balance, and can have a lost or stolen card
blocked — with a ticket raised and a written confirmation sent out of band.

AIDA owns the voice: speech-to-text, the LLM, tool calling, text-to-speech and
telephony. This repository is everything behind that — a Django service that
exposes the tools the agent calls, the database they read and write, and an
operations dashboard for watching it happen.

```
   caller ──phone──▶ AIDA (ASR · LLM · tool calling · TTS)
                        │  HTTPS + X-API-Key
                        ▼
              Django tool API  /api/v1/tools/*
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
        PostgreSQL          notification provider
   customers · accounts     console / Brevo / SMTP / Twilio
   cards · tickets · logs
```

---

## Quick start

### Docker (recommended)

```bash
cp .env.example .env
docker compose up --build
```

That brings up Postgres and the app, applies migrations, and seeds the demo
data. The dashboard is at <http://localhost:8000/dashboard/> — sign in with the
credentials printed in the startup logs (`admin` / `admin12345` by default).

### Local Python

```bash
python -m venv .venv && .venv/Scripts/activate    # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

With `DATABASE_URL` left empty the app uses a local SQLite file, so no database
server is needed for development.

### Exposing it to AIDA

AIDA needs an address it can reach from the internet. Everything for that lives
on **Dashboard → Settings → Live URL**:

1. Paste a free authtoken from
   [dashboard.ngrok.com](https://dashboard.ngrok.com/get-started/your-authtoken).
2. Press **Start tunnel**.

The public address appears a few seconds later, along with all seven tool
endpoints rebuilt on it — per-row copy buttons and a *Copy all URLs* block for
pasting into AIDA. Press **Stop tunnel** when you're done. Nothing has to be put
in `.env`, and nothing has to be restarted when the hostname changes.

The agent runs as a child process of the web container, which is what makes a
button possible without mounting the Docker socket into the app. That is a
development convenience and is treated as one: **the controls only appear, and
the start/stop actions are only accepted, when the dashboard is being browsed
from the machine running the service.** A request arriving on a public hostname
is refused server-side, not merely hidden in the template.

Two alternatives, both selectable on the same page:

- **ngrok as its own container** — for an unattended deployment, or if you would
  rather the web process could not spawn anything at all:
  `docker compose --profile ngrok up -d`. The app finds the address either way.
- **cloudflared** — no account needed, but it publishes no local API, so set mode
  to *Manual* and paste the URL from its logs:
  `docker compose --profile cloudflared up -d`

ngrok's request inspector is worth keeping open while you configure AIDA — it
shows the exact requests arriving and lets you replay one without making another
phone call. It's at <http://localhost:4040> for the dashboard-started agent, or
<http://localhost:4041> for the standalone container.

### Tests

```bash
python manage.py test
```

65 tests covering the auth gate, the failure paths, redaction, the provider
settings form, tunnel detection and its local-only gating, and the dashboard.

---

## Configuring AIDA

Open **Dashboard → Settings**. That page is the integration contract: base URL,
API key, one entry per tool with sample request/response, the function-calling
schema as JSON, and the agent system prompt. Three things to copy across:

1. **Tool endpoints** — each tool is one `POST` to a URL listed on that page.
2. **Auth header** — `X-API-Key: <your key>` on every call (an
   `Authorization: Bearer <key>` header is accepted too, for platforms that only
   allow that one).
3. **System prompt** — [`docs/aida-agent-prompt.md`](docs/aida-agent-prompt.md),
   also rendered on the Settings page with a copy button.

`GET /api/v1/tools/schema` returns the same tool definitions in the standard
OpenAI function-calling shape if AIDA can import them directly.

---

## The tools

| Tool | Endpoint | Does |
|---|---|---|
| `authenticate_caller` | `POST /api/v1/tools/authenticate` | Account number + PIN → session token |
| `get_account_balance` | `POST /api/v1/tools/get_balance` | Balance for the authenticated caller |
| `list_cards` | `POST /api/v1/tools/list_cards` | Cards on the account, so the bot can confirm which to block |
| `send_otp` | `POST /api/v1/tools/send_otp` | 6-digit step-up code |
| `verify_otp` | `POST /api/v1/tools/verify_otp` | Checks the code, elevates the session |
| `block_card` | `POST /api/v1/tools/block_card` | Blocks the card **and** opens a ticket, in one transaction |
| `send_confirmation` | `POST /api/v1/tools/send_confirmation` | Out-of-band SMS/email with name, ticket number, outcome |

Four of these are the ones the brief requires: authenticate, fetch the balance,
update the user's data (`block_card` mutates the card and writes a ticket), and
send the notification. `list_cards`, `send_otp` and `verify_otp` exist so the
agent can confirm an irreversible action before taking it.

Every response uses the same envelope:

```jsonc
// success
{"ok": true,  "tool": "get_account_balance", "data": { … }, "speech_hint": "…"}
// failure
{"ok": false, "tool": "block_card",
 "error": {"code": "CARD_ALREADY_BLOCKED", "message": "…"},
 "speech_hint": "That card is already blocked, so there's nothing more to do."}
```

### Try it without AIDA

```bash
curl -X POST http://localhost:8000/api/v1/tools/authenticate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-aida-key-change-me" \
  -d '{"account_number": "1000000001", "pin": "4821"}'
```

Take the `session_token` from the response and:

```bash
curl -X POST http://localhost:8000/api/v1/tools/get_balance \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-aida-key-change-me" \
  -d '{"session_token": "<token>"}'
```

---

## Demo accounts

| Account | PIN | Holder | Balance | Cards | Notes |
|---|---|---|---|---|---|
| `1000000001` | `4821` | Priya Raman | 4 250.75 USD | Visa ••4417, Mastercard ••9032 | two cards — the bot must ask which to block |
| `1000000002` | `7391` | Marcus Bennet | 128.40 USD | Visa ••6712 | |
| `1000000003` | `5560` | Elena Fischer | 92 310.00 CHF | Visa ••2285 | different currency, exercises the number-to-words helper |
| `5645342376` | `7284` | Sam Wilson | 15 780.25 USD | Visa ••8163 | **use this one over voice** — varied digits survive speech-to-text |
| `1000000004` | `1122` | Tomas Duarte | 0.00 USD | Visa ••5031 | **frozen** — for demonstrating a graceful refusal |

Four distinct, non-zero balances plus one account that cannot be serviced.

The `10000000xx` numbers are realistic but hostile to a voice channel: spoken
aloud they are a run of six identical digits, which speech-to-text miscounts
and a human cannot verify by ear. They are kept because they exercise the
system fine over HTTP, but `5645342376` is the one to dial with -- its digits
vary, so the recogniser has something to latch onto.
Re-run `python manage.py seed_demo` at any time to reset PINs and unlock
accounts; `--reset` also clears sessions, tickets, notifications and logs.

---

## Design choices

**Django, no DRF.** The brief asked for Django's built-in API capability. These
are plain `View` functions returning `JsonResponse`. Seven small endpoints with
one response shape don't need serializers, routers or a browsable API, and
skipping DRF keeps the dependency list to six packages.

**Authentication is enforced in the backend, not in the prompt.** This is the
part that matters most. `authenticate_caller` mints a random session token with
a 15-minute TTL; every other tool resolves that token server-side before it
does anything. An LLM that hallucinates its way past the verification step — or
a caller who insists "you already checked me" — still gets `SESSION_REQUIRED`
back. The prompt shapes *when* the agent asks for credentials; it has no say in
whether the caller is authorised.

**Account number + PIN, with OTP as step-up.** Over a phone line, digits survive
ASR far better than a spelled-out name or date of birth, and a PIN needs no
second device mid-call. Blocking a card is irreversible, so it can require a
one-time code sent to the number on file. It ships **off** so a recorded demo
runs as one uninterrupted call; turn it on for anything resembling production.

The toggle lives on **Settings → Integration → Security policy** and takes
effect immediately, because on/off here is a genuine operational choice rather
than a deployment constant: on for production, off for a continuous demo call
where an SMS round-trip would stall the conversation. Every change records who
made it — "who turned off the second factor" is worth being able to answer.
`REQUIRE_OTP_FOR_CARD_BLOCK` in `.env` still seeds the initial value.

It is the *only* security setting editable from the web UI. API keys, PIN
attempt limits, lockout duration and session TTL stay env-only and read-only,
so the dashboard cannot be used to weaken the system's own defences.

**PINs are hashed, never stored or displayed.** Django's password hashers, so
PIN handling gets the same salting and iteration treatment as passwords. There
is no screen anywhere in this app that shows a PIN. Failed attempts are counted
and the account locks for 15 minutes after three.

**Wrong account number and wrong PIN return the same error.** Distinguishing
them tells an attacker which account numbers exist.

**Blocking a card and raising its ticket share one transaction**, with the card
row locked via `select_for_update`. A caller is never told "your card is
blocked" without a ticket to quote, and never handed a ticket number for a card
that is still live. A second attempt returns `CARD_ALREADY_BLOCKED` rather than
opening a duplicate ticket.

**Notification failure does not roll back the block.** If the SMS fails, the
card stays blocked and the tool returns `NOTIFICATION_FAILED` with a
`speech_hint` telling the caller exactly that, plus the reference to write down.
Losing the confirmation message is a nuisance; silently un-blocking a stolen
card is a real loss.

**Console notifications by default, with providers configurable at runtime.**
Out of the box both channels use the `console` provider, which stores the full
message and renders it on the dashboard's Notifications page — the whole flow is
demonstrable with no third-party account. Real delivery is a dropdown away on
**Settings → Providers**: Brevo (one API key covers both email and SMS), SMTP,
or Twilio for SMS. Same code path, same `Notification` rows.

Provider credentials live in a database row rather than only in `.env`, because
they are the one piece of configuration an operator legitimately needs to change
without a redeploy, and because a "send test message" button is worth far more
than a documented environment variable. `.env` still seeds the row on first run,
so a container deployed with `BREVO_API_KEY` set works with no clicking. The
security-relevant settings — API keys, PIN attempt limits, session TTL — stay
env-only and read-only in the UI, so the web interface cannot loosen the
system's own defences.

The trade-off worth naming: those credentials sit in the database in the clear.
That is the same trust boundary as the `.env` file they came from — anyone with
read access to either has them — but it does make the database credential
material, so back it up accordingly. Secrets are write-only in the form: stored
values are never rendered back into the page, and submitting a blank field keeps
what is already there rather than wiping it.

**The public URL is discovered, not configured.** A tunnel hostname changes on
every agent restart, and re-editing `.env` and restarting the app each time is
the kind of friction that ends up costing a demo. The app queries the ngrok
agent's local API instead and caches the answer, falling back to the last known
address if the agent is briefly down, then to `PUBLIC_BASE_URL`, then to the
host being browsed. It tries both an in-container agent and the compose
service, so switching between them needs no configuration change. Nothing here
is ngrok-specific beyond that one lookup — manual mode covers cloudflared and
real deployments.

**Starting the tunnel from the dashboard is gated on the request being local.**
Letting a web process spawn other processes is fine on a laptop and wrong on a
public host, so the ngrok controls are hidden — and, more importantly, the
start/stop actions are refused — for any request arriving on a non-local
hostname. The alternative was mounting the Docker socket into the web
container so it could start a sibling container; that hands the app effective
root on the host, which is a much worse trade for a convenience button.

**Every response carries speech.** TTS engines mangle `4250.75 USD` and
`TKT-260904-K7QF2M`. Tools therefore return `balance_spoken`
("four thousand two hundred and fifty dollars and seventy-five cents") and
`ticket_number_spoken` (spelled character by character), and ticket numbers use
an alphabet with no `0`/`O`, `1`/`I`, `5`/`S` or `8`/`B`. Errors carry a
`speech_hint` too, so the bot's failure handling is consistent regardless of how
the model would have phrased it.

**Every tool call is logged twice** — a one-line structured record on stdout
(visible in `docker compose logs -f web`) and a `ToolCallLog` row with the full
request and response, browsable at Dashboard → Tool logs. PINs, OTP codes and
session tokens are redacted before either is written.

**PostgreSQL in compose, SQLite locally.** `DATABASE_URL` decides. Postgres for
anything shipped, because tickets and blocked cards must survive a restart;
SQLite so a reviewer can clone and run with one command.

---

## Dashboard

| Page | What it's for |
|---|---|
| **Overview** | Counts, 24-hour tool usage, recent calls and tickets |
| **Customers** | Directory, searchable; drill into one customer's full history |
| **Accounts** | The balance table the bot reads, plus lockout state |
| **Tickets** | Every card block, its reason, and whether confirmation went out |
| **Notifications** | The outbox, including full message bodies under the console provider |
| **Tool logs** | Per-call audit trail, filterable by tool and by failure |
| **Settings → Integration** | URLs, API key, schema, agent prompt, security policy, runtime configuration |
| **Settings → Providers** | Email/SMS credentials, with a send-test button |
| **Settings → Live URL** | Start/stop the tunnel; the public address and every endpoint built on it |

Staff-only. Django admin lives at `/django-admin/` for raw edits — changing a
balance, resetting a PIN, unlocking an account.

Settings are shown read-only on purpose: they come from `.env`, so a running
container cannot be reconfigured through the web UI.

---

## Connecting a real email / SMS account

Both OTP codes and card-block confirmations go out through whichever provider is
selected. Configure it at **Dashboard → Settings → Providers** — changes apply
immediately, no restart. Every field can also be seeded from `.env` (see
`.env.example`) if you would rather bake it into a deployment.

### Brevo — email and SMS from one account

1. Brevo → **SMTP & API → API keys** → create a key (it starts `xkeysib-`).
2. Brevo → **Senders** → verify the address you want mail to come from.
3. On the Providers page set **Email provider** and/or **SMS provider** to
   *Brevo*, paste the API key, set the sender email, and save.
4. Press **Send test** with your own address or number.

Sent via `POST https://api.brevo.com/v3/smtp/email` and
`POST https://api.brevo.com/v3/transactionalSMS/sms`. For SMS, note that Brevo
needs SMS credits on the account, and that alphanumeric sender IDs (`AIDABank`)
are restricted or forbidden in some countries — the US among them, where a
purchased number is required instead.

### SMTP — any mail server

Host, port, username, password, from-address. Brevo's own relay is
`smtp-relay.brevo.com:587` using your Brevo login and an SMTP key as the
password, which is a good fallback if the API is blocked.

### Twilio — SMS

Account SID, auth token and a from-number in E.164 from the Twilio console.
Sent via the Messages REST API. On a Twilio trial account the destination must
be a verified number, and messages arrive with a trial prefix.

### Brevo, the three things that will waste your afternoon

All three fail *silently* or misleadingly, and none of them are visible from the
sending side, so they are worth knowing before you start:

1. **An SMTP key is not an API key.** Brevo issues both from the same
   *SMTP & API* page, on different tabs. SMTP keys start `xsmtpsib-` and work
   only on the relay; the REST API needs an `xkeysib-` key from the
   *API keys & MCP* tab and answers anything else with
   `401 {"message":"Key not found"}`. Brevo SMS is API-only, so the SMTP key
   cannot send text messages either.

2. **SMTP is IP-allowlisted by default.** Sending from an unlisted address
   fails at `AUTH` with `525 5.7.1 Unauthorized IP address`. Add the machine's
   outbound IP under the banner on that same page. This one at least fails
   loudly.

3. **The `From` address must be a verified sender**, and this is the trap. The
   SMTP *login* (`b48673001@smtp-brevo.com`) looks like an address and is
   accepted by the relay -- you get `250 OK: queued` with a message id -- and
   then Brevo drops the message. Nothing in the SMTP conversation reveals it.
   Verify the sender under *Senders, domains, IPs* and use that address.

The symptom of 1 is an immediate 401, of 2 an immediate 525, and of 3 a
perfectly successful send that never arrives. If mail is accepted but not
delivered, check 3 first.

### If delivery fails

Nothing is hidden. The attempt is written to the Notifications page with
`status=failed` and the provider's own error message, and the agent is handed a
`NOTIFICATION_FAILED` error whose `speech_hint` tells the caller the card is
still blocked and reads the ticket number back. A card block is never rolled
back because a text message didn't send.

---

## Layout

```
config/          Django project — settings, URLs, WSGI
bank/            Domain: models, services (all business logic), notifications,
                 speech helpers, seed command
api/             HTTP surface: views, tool schema, API-key auth, tests
dashboard/       Staff UI: views, templates, styles
docs/            Agent system prompt
compose.yml      Postgres + app (+ optional ngrok / cloudflared tunnels)
```

The views in `api/` stay thin — parse, delegate to `bank/services.py`,
serialise. Authorisation decisions live in the service layer, in one place,
where they can be tested directly.

---

## Environment

Everything is read from the environment; see `.env.example` for the full list.
The ones that matter:

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_API_KEYS` | `dev-aida-key-change-me` | Comma-separated; rotate without downtime |
| `NGROK_AUTHTOKEN` | — | Optional seed for the dashboard's authtoken field |
| `PUBLIC_BASE_URL` | — | Fallback public URL when no tunnel is running |
| `DATABASE_URL` | — | Empty → SQLite; compose sets Postgres |
| `EMAIL_PROVIDER` | `console` | `console` \| `smtp` \| `brevo` — seeds the editable row |
| `SMS_PROVIDER` | `console` | `console` \| `twilio` \| `brevo` — seeds the editable row |
| `BREVO_API_KEY` | — | One key for Brevo email **and** SMS |
| `REQUIRE_OTP_FOR_CARD_BLOCK` | `false` | Seeds the step-up toggle; change it later in the dashboard |
| `MAX_PIN_ATTEMPTS` / `LOCKOUT_MINUTES` | `3` / `15` | Brute-force protection |
| `AUTH_SESSION_TTL_SECONDS` | `900` | How long a verified call stays verified |

Change `DJANGO_SECRET_KEY`, `TOOL_API_KEYS` and the admin password before this
is reachable from the internet.

---

## What I'd improve with more time

The session token is currently the only binding between a verified caller and
the tools — I'd tie it to AIDA's call ID and the ANI as well, so a leaked token
is useless outside the call it was issued on, and add rate limiting per caller
ID rather than only per account. Balances are read straight off the `accounts`
row; a real integration would go through a core-banking adapter with an
idempotency key on every write, which also lets `block_card` be retried safely
when the network drops mid-call. Notification delivery is synchronous inside the
request, so a slow SMS provider adds dead air to the call — that belongs in a
task queue with retries, with the agent told the message is *queued* rather than
*sent*. I'd also add a proper evaluation harness: recorded audio fixtures driven
through the agent, asserting that no balance is ever spoken before an
`authenticate_caller` success appears in the tool log, since that is the one
failure mode where a passing manual demo proves very little.
