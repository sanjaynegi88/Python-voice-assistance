"""
Machine-readable description of the tools exposed to the voice agent.

One source of truth, used three ways:
  * GET /api/v1/tools/schema      -- paste straight into AIDA's tool config
  * the dashboard Settings page   -- shows URLs, params and samples
  * the README                    -- generated from the same list
"""

TOOLS = [
    {
        "name": "authenticate_caller",
        "path": "tools/authenticate",
        "method": "POST",
        "requirement": "2, 3",
        "summary": (
            "Verify the caller with their account number and 4-digit PIN. Returns a "
            "session_token that every other tool requires. Call this before any tool "
            "that touches account data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account_number": {
                    "type": "string",
                    "description": "The caller's account number, digits only.",
                },
                "pin": {
                    "type": "string",
                    "description": "The caller's 4-digit PIN, digits only.",
                },
                "caller_id": {
                    "type": "string",
                    "description": "Optional caller ID / ANI reported by the telephony layer.",
                },
            },
            "required": ["account_number", "pin"],
        },
        "sample_request": {"account_number": "1000000001", "pin": "4821"},
        "sample_response": {
            "ok": True,
            "tool": "authenticate_caller",
            "data": {
                "authenticated": True,
                "session_token": "s2Yb...redacted",
                "customer_name": "Priya Raman",
                "first_name": "Priya",
                "account_number_masked": "****0001",
                "expires_in_seconds": 900,
                "speech_hint": "Thanks Priya, you're verified.",
            },
        },
    },
    {
        "name": "get_account_balance",
        "path": "tools/get_balance",
        "method": "POST",
        "requirement": "4",
        "summary": (
            "Return the authenticated caller's current balance. Read balance_spoken "
            "aloud rather than the raw number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {
                    "type": "string",
                    "description": "Token returned by authenticate_caller.",
                }
            },
            "required": ["session_token"],
        },
        "sample_request": {"session_token": "s2Yb...redacted"},
        "sample_response": {
            "ok": True,
            "tool": "get_account_balance",
            "data": {
                "customer_name": "Priya Raman",
                "account_number_masked": "****0001",
                "balance": "4250.75",
                "currency": "USD",
                "balance_spoken": "four thousand two hundred and fifty dollars and seventy-five cents",
                "speech_hint": "Your checking account ending four four one seven ...",
            },
        },
    },
    {
        "name": "list_cards",
        "path": "tools/list_cards",
        "method": "POST",
        "requirement": "5",
        "summary": (
            "List the cards on the account so you can confirm with the caller which "
            "one to block before doing anything irreversible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {"type": "string", "description": "Session token."}
            },
            "required": ["session_token"],
        },
        "sample_request": {"session_token": "s2Yb...redacted"},
        "sample_response": {
            "ok": True,
            "tool": "list_cards",
            "data": {
                "cards": [
                    {
                        "last4": "4417",
                        "brand": "Visa",
                        "type": "Debit",
                        "status": "active",
                        "label": "Visa debit ending 4417",
                    }
                ],
                "speech_hint": "I can see one active card ...",
            },
        },
    },
    {
        "name": "send_otp",
        "path": "tools/send_otp",
        "method": "POST",
        "requirement": "2 (step-up)",
        "summary": (
            "Send a 6-digit one-time code to the contact details on file. Only needed "
            "when a tool replies with error code OTP_REQUIRED."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {"type": "string", "description": "Session token."},
                "channel": {
                    "type": "string",
                    "enum": ["sms", "email"],
                    "description": "Where to send the code. Defaults to SMS.",
                },
            },
            "required": ["session_token"],
        },
        "sample_request": {"session_token": "s2Yb...redacted", "channel": "sms"},
        "sample_response": {
            "ok": True,
            "tool": "send_otp",
            "data": {"otp_sent": True, "channel": "sms", "destination_masked": "+41*****2233"},
        },
    },
    {
        "name": "verify_otp",
        "path": "tools/verify_otp",
        "method": "POST",
        "requirement": "2 (step-up)",
        "summary": "Check the 6-digit code the caller read back and elevate the session.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {"type": "string", "description": "Session token."},
                "code": {"type": "string", "description": "The 6 digits the caller said."},
            },
            "required": ["session_token", "code"],
        },
        "sample_request": {"session_token": "s2Yb...redacted", "code": "418902"},
        "sample_response": {
            "ok": True,
            "tool": "verify_otp",
            "data": {"verified": True, "level": "elevated"},
        },
    },
    {
        "name": "block_card",
        "path": "tools/block_card",
        "method": "POST",
        "requirement": "5, 6",
        "summary": (
            "Irreversibly block a card and open a ticket against the account. Confirm "
            "the card and the reason with the caller BEFORE calling this. Returns the "
            "ticket number to read back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {"type": "string", "description": "Session token."},
                "card_last4": {
                    "type": "string",
                    "description": "Last 4 digits of the card to block.",
                },
                "reason": {
                    "type": "string",
                    "enum": ["lost", "stolen", "compromised", "damaged", "other"],
                    "description": "Why the caller wants the card blocked.",
                },
                "incident_description": {
                    "type": "string",
                    "description": "What the caller said about the loss or compromise.",
                },
                "incident_date": {
                    "type": "string",
                    "description": "Date of the incident, YYYY-MM-DD, if the caller gives one.",
                },
            },
            "required": ["session_token", "card_last4", "reason"],
        },
        "sample_request": {
            "session_token": "s2Yb...redacted",
            "card_last4": "4417",
            "reason": "stolen",
            "incident_description": "Wallet taken on the tram this morning.",
        },
        "sample_response": {
            "ok": True,
            "tool": "block_card",
            "data": {
                "blocked": True,
                "ticket_number": "TKT-260904-K7QF2M",
                "ticket_number_spoken": "T K T, dash, two six zero nine zero four, dash, K seven Q F two M",
                "card_last4": "4417",
            },
        },
    },
    {
        "name": "send_confirmation",
        "path": "tools/send_confirmation",
        "method": "POST",
        "requirement": "7",
        "summary": (
            "Send the out-of-band confirmation (name, ticket number, confirmation that "
            "the card is blocked) by SMS or email. Call this straight after block_card."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_token": {"type": "string", "description": "Session token."},
                "ticket_number": {
                    "type": "string",
                    "description": "Ticket number returned by block_card.",
                },
                "channel": {
                    "type": "string",
                    "enum": ["sms", "email"],
                    "description": "Delivery channel. Defaults to SMS.",
                },
            },
            "required": ["session_token", "ticket_number"],
        },
        "sample_request": {
            "session_token": "s2Yb...redacted",
            "ticket_number": "TKT-260904-K7QF2M",
            "channel": "sms",
        },
        "sample_response": {
            "ok": True,
            "tool": "send_confirmation",
            "data": {
                "sent": True,
                "channel": "sms",
                "destination_masked": "+41*****2233",
                "ticket_number": "TKT-260904-K7QF2M",
            },
        },
    },
]


def function_definitions():
    """OpenAI-style function specs, the format most agent platforms accept."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["summary"],
                "parameters": tool["parameters"],
            },
        }
        for tool in TOOLS
    ]


def endpoints(base_url=""):
    """The same list with absolute URLs, for the dashboard and the README."""
    base = base_url.rstrip("/")
    return [
        {
            **tool,
            "url": f"{base}/api/v1/{tool['path']}" if base else f"/api/v1/{tool['path']}",
        }
        for tool in TOOLS
    ]
