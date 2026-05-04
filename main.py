"""
Tradie Receptionist - SMS webhook handler
Version 0.6 - Layers 5 + 6: multi-turn memory + urgent owner escalation
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape as xml_escape
from typing import Optional

import gspread
from anthropic import Anthropic
from flask import Flask, request, Response
from google.oauth2.service_account import Credentials
from twilio.rest import Client as TwilioClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("receptionist")

app = Flask(__name__)

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_CREDS_PATH = "/etc/secrets/google-credentials.json"

ASSISTANT_NAME = "Joe"
CONVERSATION_LOG_TAB = "Conversation Log"
CONVERSATION_LOG_HEADERS = [
    "timestamp", "business_name", "from_number", "to_number",
    "message", "reply", "urgent",
]
URGENT_TAG = "##URGENT##"

# Conversation memory window: last N round-trips OR last X hours, whichever is shorter.
HISTORY_TURN_LIMIT = 6
HISTORY_HOURS_LIMIT = 24


# ===========================================================================
# Prompt template
# ===========================================================================

SMS_INSTRUCTION = (
    "IMPORTANT: This conversation is over SMS. Keep every reply under "
    "320 characters (about 2 SMS segments). Be concise but warm. Each reply "
    "should move the customer toward either (a) a quoted price, or "
    "(b) a booked on-site visit via the booking link. Ask only for the "
    "minimum info needed.\n"
    "\n"
    "Do NOT use emojis. Do NOT use exclamation-heavy or overly casual "
    "language. Plain professional Aussie tone — friendly but no theatrics."
)

REPLY_PLAYBOOK = (
    "When a customer messages, your job is to:\n"
    "1. Greet warmly and confirm we cover what they need (only on the FIRST "
    "message in a conversation — if there is prior history, skip the greeting "
    "and continue naturally)\n"
    "2. Either provide a price/range if it's a standard job, OR send the "
    "booking link for an on-site quote\n"
    "3. Collect: their name, suburb, brief description of the job\n"
    "4. If genuinely urgent (gas leak, flooding, no power, electrical "
    "danger, etc.), end your reply with ##URGENT## on its own line\n"
    "\n"
    "Be honest. Do not invent prices outside the ranges given. If you don't "
    "know something, say you'll get the tradie to confirm."
)


def _build_system_prompt(tradie: dict) -> str:
    biz = (tradie.get("business_name") or "the business").strip()
    trade = (tradie.get("trade_type") or "tradie").strip()
    area = (tradie.get("service_area") or "the local area").strip()

    parts = [f"You are {ASSISTANT_NAME}, the receptionist for {biz}, a {trade} servicing {area}."]

    def add_if(label: str, value) -> None:
        if value and str(value).strip():
            parts.append(f"{label}: {str(value).strip()}")

    add_if("Services we offer", tradie.get("services_offered"))
    add_if("What we don't do", tradie.get("does_not_service"))
    add_if("Hours", tradie.get("hours"))
    add_if("After hours", tradie.get("after_hours_policy"))
    add_if("Callout fee", tradie.get("callout_fee"))
    add_if("Pricing", tradie.get("pricing_notes"))
    add_if("Booking link", tradie.get("cal_link"))

    parts.append("")
    parts.append(REPLY_PLAYBOOK)
    parts.append("")
    parts.append(SMS_INSTRUCTION)

    return "\n".join(parts)


# ===========================================================================
# Sheets / Anthropic / Twilio clients
# ===========================================================================

def _build_sheets_client() -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    return gspread.authorize(creds)


try:
    sheets_client = _build_sheets_client()
    spreadsheet = sheets_client.open_by_key(SHEET_ID)
    log.info("Sheets client initialised. Spreadsheet: %s", spreadsheet.title)
except Exception as exc:
    log.exception("Failed to initialise Sheets client: %s", exc)
    sheets_client = None
    spreadsheet = None

try:
    anthropic_client = Anthropic()
    log.info("Anthropic client initialised.")
except Exception as exc:
    log.exception("Failed to initialise Anthropic client: %s", exc)
    anthropic_client = None

try:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    log.info("Twilio client initialised.")
except Exception as exc:
    log.exception("Failed to initialise Twilio client: %s", exc)
    twilio_client = None


# ===========================================================================
# Phone normalisation
# ===========================================================================

def _normalise_phone(value) -> str:
    s = str(value).strip()
    if s and not s.startswith("+") and s.isdigit():
        s = "+" + s
    return s


# ===========================================================================
# Conversation Log (Layer 4) + history retrieval (Layer 5)
# ===========================================================================

def _ensure_conversation_log_tab():
    if spreadsheet is None:
        return None
    try:
        return spreadsheet.worksheet(CONVERSATION_LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Conversation Log tab not found; creating it")
        try:
            tab = spreadsheet.add_worksheet(
                title=CONVERSATION_LOG_TAB,
                rows=1000,
                cols=len(CONVERSATION_LOG_HEADERS),
            )
            tab.update(
                values=[CONVERSATION_LOG_HEADERS],
                range_name=f"A1:{chr(64 + len(CONVERSATION_LOG_HEADERS))}1",
            )
            log.info("Created Conversation Log tab with headers")
            return tab
        except Exception as exc:
            log.exception("Failed to create Conversation Log tab: %s", exc)
            return None
    except Exception as exc:
        log.exception("Failed to access Conversation Log tab: %s", exc)
        return None


def get_conversation_history(from_number: str, to_number: str) -> list[dict]:
    """Return alternating user/assistant message dicts for this conversation pair,
    capped at HISTORY_TURN_LIMIT turns and HISTORY_HOURS_LIMIT hours.
    """
    tab = _ensure_conversation_log_tab()
    if tab is None:
        return []

    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Conversation Log for history: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HISTORY_HOURS_LIMIT)

    matching = []
    for row in rows:
        if str(row.get("from_number", "")).strip() != from_number:
            continue
        if str(row.get("to_number", "")).strip() != to_number:
            continue
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]).strip())
        except (ValueError, KeyError, TypeError):
            continue
        if ts < cutoff:
            continue
        matching.append(row)

    matching = matching[-HISTORY_TURN_LIMIT:]

    messages = []
    for row in matching:
        msg = str(row.get("message", "")).strip()
        rep = str(row.get("reply", "")).strip()
        if msg and rep:  # only include complete turns; preserves alternation
            messages.append({"role": "user", "content": msg})
            messages.append({"role": "assistant", "content": rep})

    log.info("Loaded %d historical turns for %s -> %s", len(messages) // 2, from_number, to_number)
    return messages


def log_conversation(
    business_name: str,
    from_number: str,
    to_number: str,
    message: str,
    reply: str,
    is_urgent: bool,
) -> None:
    """Append a turn to the Conversation Log. Best-effort."""
    tab = _ensure_conversation_log_tab()
    if tab is None:
        log.error("Cannot log conversation: tab unavailable")
        return

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        tab.append_row(
            [timestamp, business_name, from_number, to_number, message, reply,
             "TRUE" if is_urgent else "FALSE"],
            value_input_option="USER_ENTERED",
        )
        log.info("Conversation logged (urgent=%s)", is_urgent)
    except Exception as exc:
        log.exception("Failed to append to Conversation Log: %s", exc)


# ===========================================================================
# Owner escalation (Layer 6)
# ===========================================================================

def send_owner_alert(
    tradie: dict,
    customer_number: str,
    customer_message: str,
    joe_reply: str,
) -> None:
    """SMS the tradie's owner_mobile when a conversation is flagged urgent. Best-effort."""
    if twilio_client is None:
        log.error("Cannot send urgent alert: Twilio client not initialised")
        return

    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    biz = tradie.get("business_name") or "the business"

    if not owner or not twilio_from:
        log.error("Cannot send urgent alert: missing owner_mobile (%r) or phone_number (%r)",
                  owner, twilio_from)
        return

    body = (
        f"URGENT — {biz}\n"
        f"Customer: {customer_number}\n"
        f"Said: {customer_message}\n"
        f"Joe replied: {joe_reply}"
    )

    try:
        twilio_client.messages.create(body=body, from_=twilio_from, to=owner)
        log.info("Urgent alert sent to owner %s", owner)
    except Exception as exc:
        log.exception("Failed to send urgent alert: %s", exc)


# ===========================================================================
# Tradie lookup
# ===========================================================================

def find_tradie(to_number: str) -> Optional[dict]:
    if spreadsheet is None:
        log.error("Sheets client not initialised; cannot look up tradie")
        return None

    try:
        client_tab = spreadsheet.worksheet("Client")
        rows = client_tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Client tab: %s", exc)
        return None

    target = _normalise_phone(to_number)
    log.info("find_tradie searching for to_number=%r among %d rows", target, len(rows))

    for i, row in enumerate(rows):
        sheet_phone = _normalise_phone(row.get("phone_number", ""))
        if sheet_phone == target:
            if str(row.get("active", "")).strip().upper() != "TRUE":
                log.info("  matched row %d but active!=TRUE; ignoring", i)
                return None
            log.info("  matched row %d: %r", i, row.get("business_name", ""))
            return row

    log.info("  no row matched")
    return None


# ===========================================================================
# Reply generation
# ===========================================================================

def generate_reply(tradie: dict, user_message: str, history: list[dict]) -> str:
    business = tradie.get("business_name") or "the business"
    fallback = f"Hi, thanks for messaging {business}. We'll get back to you shortly."

    if anthropic_client is None:
        log.error("Anthropic client not initialised; using fallback reply")
        return fallback

    system_prompt = _build_system_prompt(tradie)
    messages = history + [{"role": "user", "content": user_message}]

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system_prompt,
            messages=messages,
        )
        text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        reply = "".join(text_parts).strip()
        log.info("Claude reply (%d chars): %r", len(reply), reply)
        return reply or fallback
    except Exception as exc:
        log.exception("Anthropic API call failed: %s", exc)
        return fallback


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/", methods=["GET"])
def health() -> str:
    return "Tradie Receptionist v0.6 - alive (Layers 5+6: history + urgent escalation)"


@app.route("/test", methods=["GET"])
def test() -> str:
    to = request.args.get("to", "")
    if not to:
        return "Pass ?to=+61... to look up a tradie"
    tradie = find_tradie(to)
    if tradie:
        return f"Matched: {tradie.get('business_name', '(no name)')}"
    return f"No tradie matched for {to}"


@app.route("/test/prompt", methods=["GET"])
def test_prompt() -> Response:
    to = request.args.get("to", "")
    if not to:
        return Response("Pass ?to=+61... to view the assembled prompt", mimetype="text/plain")
    tradie = find_tradie(to)
    if not tradie:
        return Response(f"No tradie matched for {to}", mimetype="text/plain")
    return Response(_build_system_prompt(tradie), mimetype="text/plain")


@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    sid = request.form.get("MessageSid", "")
    body = request.form.get("Body", "")
    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, sid, body,
    )

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie found in Client tab for To=%s", to_number)
        twiml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<Response><Message>Sorry, this number isn't configured yet.</Message></Response>"
        )
        return Response(twiml, mimetype="application/xml")

    business = tradie.get("business_name", "your tradie")
    log.info("Tradie matched: %s (%s)", business, to_number)

    # Layer 5 — pull recent history for this conversation pair
    history = get_conversation_history(from_number, to_number)

    # Generate reply with history as context
    raw_reply = generate_reply(tradie, body, history)

    # Detect & strip the urgent tag (customer never sees it)
    is_urgent = URGENT_TAG in raw_reply
    clean_reply = raw_reply.replace(URGENT_TAG, "").strip()

    # Log the turn (Layer 4)
    log_conversation(business, from_number, to_number, body, clean_reply, is_urgent)

    # Layer 6 — escalate to owner if urgent
    if is_urgent:
        send_owner_alert(tradie, from_number, body, clean_reply)

    twiml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{xml_escape(clean_reply)}</Message></Response>"
    )
    return Response(twiml, mimetype="application/xml")
