# Recording the demo call

A 1–2 minute call that hits every item on the evaluation checklist. Have the
dashboard's **Tool logs** page open in a second window and screen-record both —
that is what makes "the LLM uses tool calls" verifiable.

## Before you dial

```bash
docker compose up -d
python manage.py seed_demo --reset     # clean logs, unlock accounts
```

Confirm `PUBLIC_BASE_URL` matches your live tunnel and that AIDA's tool
configuration points at it (Dashboard → Settings).

## The call

**1 — Wrong credentials first (≈20s).** This is its own checklist row, so get it
out of the way early.

> "Hi, I'd like my balance please."
> — *bot asks for the account number*
> "One zero zero zero zero zero zero zero zero one."
> — *bot asks for the PIN*
> "One one one one."
> — *bot should decline and offer another attempt, without saying which field
> was wrong, and without revealing anything about the account*

**2 — Authenticate and read the balance (≈30s).**

> "Sorry, it's four eight two one."
> — *bot verifies, greets Priya by name*
> "What's my balance?"
> — *bot: four thousand two hundred and fifty dollars and seventy-five cents*

**3 — Block a card (≈40s).**

> "I also need to block my card, my wallet was stolen this morning."
> — *bot lists both cards and asks which one*
> "The Visa, ending four four one seven."
> — *bot confirms the card and the reason, then blocks it and reads the ticket
> number back character by character*
> — *bot confirms the SMS/email has been sent*

**4 — Close.**

> "That's everything, thanks."

## Afterwards, on camera

- **Tool logs** — the sequence `authenticate_caller` (failed, then ok) →
  `get_account_balance` → `list_cards` → `block_card` → `send_confirmation`,
  with PINs shown as `***`.
- **Tickets** — the new ticket, tied to Priya's account, confirmation sent.
- **Notifications** — the message body with her name and the ticket number.
- **Accounts** — Visa ••4417 now blocked; the other three balances unchanged.

## Fallbacks if something misbehaves on the day

- Balance spoken before authentication → impossible; the backend returns
  `SESSION_REQUIRED`. Show that in the logs, it is a stronger result than a
  polished take.
- The card block fails mid-call → the ticket is rolled back with it, so retry.
- The notification fails → the card is still blocked and the bot says so. That
  is the designed behaviour, not a broken take; leave it in and mention it.
