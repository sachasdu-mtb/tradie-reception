"""
Tradie Receptionist - SMS + Voice handler
Version 0.8.3 - randomised caller handoff delay (60-90s)
"""

import logging
import os
import random
from datetime import datetime, timedelta, timezone
from html import escape as xml_escape
from threading import Lock, Timer
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

VOICE_NAME = "Google.en-AU-Neural2-C"
VOICE_LANG = "en-AU"
SPEECH_TIMEOUT_SECONDS = "2"  # seconds of silence before Twilio considers speech over

CONVERSATION_LOG_TAB = "Conversation Log"
CONVERSATION_LOG_HEADERS = [
    "timestamp", "business_name", "from_number", "to_number",
    "message", "reply", "urgent",
]

MUTES_TAB = "Mutes"
MUTES_HEADERS = ["timestamp", "business_name", "customer_number", "expires_at"]

URGENT_TAG = "##URGENT##"
END_TAG = "##END##"
MUTE_HOURS = 24

HISTORY_TURN_LIMIT = 6
HISTORY_HOURS_LIMIT = 24

VOICE_TURN_LIMIT = 5

# Random delay before the post-call SMS reaches the caller. A real receptionist
# wouldn't text you 0.4 seconds after hanging up — randomising avoids the
# tell-tale "robot ping" cadence. Range is in seconds.
CALLER_HANDOFF_DELAY_MIN_SECONDS = 60
CALLER_HANDOFF_DELAY_MAX_SECONDS = 90

_voice_state: dict[str, dict] = {}
_voice_state_lock = Lock()


# ===========================================================================
# Prompts
# ===========================================================================

GUARDRAILS = (
    "STRICT RULES — these override anything else:\n"
    "- Use ONLY the services explicitly listed in 'Services we offer'. "
    "Do NOT expand abbreviations, do NOT infer related services, do NOT "
    "guess at what the trade type might include. If a service isn't on the "
    "list, you don't offer it.\n"
    "- If a customer asks about something not on the list, say you'll need "
    "to check with the tradie and offer to take their details.\n"
    "- Do NOT invent prices, fees, timeframes, qualifications, or guarantees "
    "that aren't written above.\n"
    "- Do NOT speculate about the meaning of trade names or abbreviations. "
    "If unsure what the customer means, ask them rather than guess."
)

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

SMS_PLAYBOOK = (
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
    "Be honest. If you don't know something, say you'll get the tradie to confirm."
)

VOICE_INSTRUCTION = (
    "IMPORTANT: You are on a phone call with the customer right now. Speak "
    "naturally and concisely — 1 to 2 short sentences per turn, never more. "
    "Aussie tone, friendly and professional.\n"
    "\n"
    "Do NOT use formatting, lists, asterisks, headers, or symbols. Plain "
    "spoken English only. No emojis or special characters — your words will "
    "be read aloud.\n"
    "\n"
    "Speak numbers, prices, and times as a person would say them out loud "
    "(e.g. 'one hundred dollars' not '$100'; 'nine o'clock' not '9:00').\n"
    "\n"
    "Do NOT try to capture customer names, suburbs, addresses, or specific "
    "job details over the phone — phone speech recognition gets these wrong. "
    "Get them via the SMS handoff (see playbook)."
)

VOICE_PLAYBOOK = (
    "You are answering an inbound phone call. The system has already greeted "
    "the caller — do NOT greet them again. Your job is to:\n"
    "\n"
    "1. Listen to the customer explain their issue\n"
    "2. Briefly confirm we cover that kind of work (one short sentence)\n"
    "3. Ask them to text this same number with their name, suburb, and a "
    "brief description of the job, and tell them the tradie will call them "
    "back within the hour\n"
    "4. End your reply with ##END## on its own line so the call wraps cleanly\n"
    "\n"
    "Example reply: 'Yep, we handle hot water systems all the time. To make "
    "sure we get your details right, could you text us at this number with "
    "your name, suburb, and a quick description of the job? The tradie will "
    "give you a call back within the hour. ##END##'\n"
    "\n"
    "If the customer asks something that isn't on the services list, say you'll "
    "check with the tradie and ask them to text the same details — then ##END##.\n"
    "\n"
    "If genuinely urgent (gas leak, flooding, no power, electrical danger, "
    "sewerage overflow, etc.) end your reply with ##URGENT## instead of "
    "##END## — the system will try to transfer the caller live.\n"
    "\n"
    "Do NOT try to collect details verbally. Do NOT quote prices. Do NOT "
    "promise specific times beyond 'within the hour'."
)


def _build_system_prompt(tradie: dict, channel: str) -> str:
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
    if channel == "sms":
        add_if("Booking link", tradie.get("cal_link"))

    parts.append("")
    parts.append(GUARDRAILS)
    parts.append("")
    if channel == "voice":
        parts.append(VOICE_PLAYBOOK)
        parts.append("")
        parts.append(VOICE_INSTRUCTION)
    else:
        parts.append(SMS_PLAYBOOK)
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
# Worksheet helpers
# ===========================================================================

def _ensure_tab(name: str, headers: list[str]):
    if spreadsheet is None:
        return None
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        log.info("%s tab not found; creating it", name)
        try:
            tab = spreadsheet.add_worksheet(title=name, rows=1000, cols=len(headers))
            tab.update(values=[headers], range_name=f"A1:{chr(64 + len(headers))}1")
            log.info("Created %s tab with headers", name)
            return tab
        except Exception as exc:
            log.exception("Failed to create %s tab: %s", name, exc)
            return None
    except Exception as exc:
        log.exception("Failed to access %s tab: %s", name, exc)
        return None


# ===========================================================================
# Conversation Log + history
# ===========================================================================

def get_conversation_history(from_number: str, to_number: str) -> list[dict]:
    tab = _ensure_tab(CONVERSATION_LOG_TAB, CONVERSATION_LOG_HEADERS)
    if tab is None:
        return []

    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Conversation Log for history: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HISTORY_HOURS_LIMIT)
    target_from = _normalise_phone(from_number)
    target_to = _normalise_phone(to_number)

    matching = []
    for row in rows:
        row_from = _normalise_phone(row.get("from_number", ""))
        row_to = _normalise_phone(row.get("to_number", ""))
        if row_from != target_from or row_to != target_to:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("timestamp", "")).strip())
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        matching.append(row)

    matching = matching[-HISTORY_TURN_LIMIT:]

    messages = []
    for row in matching:
        msg = str(row.get("message", "")).strip()
        rep = str(row.get("reply", "")).strip()
        if msg and rep:
            messages.append({"role": "user", "content": msg})
            messages.append({"role": "assistant", "content": rep})

    log.info("Loaded %d historical turns for %s -> %s",
             len(messages) // 2, target_from, target_to)
    return messages


def log_conversation(
    business_name: str,
    from_number: str,
    to_number: str,
    message: str,
    reply: str,
    is_urgent: bool,
) -> None:
    tab = _ensure_tab(CONVERSATION_LOG_TAB, CONVERSATION_LOG_HEADERS)
    if tab is None:
        log.error("Cannot log conversation: tab unavailable")
        return

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        tab.append_row(
            [timestamp, business_name, from_number, to_number, message, reply,
             "TRUE" if is_urgent else "FALSE"],
            value_input_option="RAW",
        )
        log.info("Conversation logged (urgent=%s)", is_urgent)
    except Exception as exc:
        log.exception("Failed to append to Conversation Log: %s", exc)


# ===========================================================================
# Mutes
# ===========================================================================

def is_muted(business_name: str, customer_number: str) -> bool:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return False
    try:
        rows = tab.get_all_records()
    except Exception as exc:
        log.exception("Failed to read Mutes: %s", exc)
        return False
    target_customer = _normalise_phone(customer_number)
    now = datetime.now(timezone.utc)
    for row in rows:
        if str(row.get("business_name", "")).strip() != business_name:
            continue
        if _normalise_phone(row.get("customer_number", "")) != target_customer:
            continue
        try:
            expires = datetime.fromisoformat(str(row.get("expires_at", "")).strip())
        except (ValueError, TypeError):
            continue
        if expires > now:
            return True
    return False


def add_mute(business_name: str, customer_number: str) -> Optional[datetime]:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return None
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=MUTE_HOURS)
    try:
        tab.append_row(
            [now.isoformat(timespec="seconds"), business_name, customer_number,
             expires.isoformat(timespec="seconds")],
            value_input_option="RAW",
        )
        log.info("Mute added: %s for %s until %s", business_name, customer_number, expires)
        return expires
    except Exception as exc:
        log.exception("Failed to add mute: %s", exc)
        return None


def expire_mutes(business_name: str, customer_number: str) -> int:
    tab = _ensure_tab(MUTES_TAB, MUTES_HEADERS)
    if tab is None:
        return 0
    try:
        rows = tab.get_all_values()
    except Exception as exc:
        log.exception("Failed to read Mutes for expiry: %s", exc)
        return 0
    if not rows or len(rows) < 2:
        return 0
    target_customer = _normalise_phone(customer_number)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    count = 0
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) < 4:
            continue
        if str(row[1]).strip() != business_name:
            continue
        if _normalise_phone(row[2]) != target_customer:
            continue
        try:
            expires = datetime.fromisoformat(str(row[3]).strip())
        except (ValueError, TypeError):
            continue
        if expires <= now:
            continue
        try:
            tab.update_cell(idx, 4, now_iso)
            count += 1
        except Exception as exc:
            log.exception("Failed to expire mute on row %d: %s", idx, exc)
    log.info("Expired %d active mute(s) for %s/%s", count, business_name, target_customer)
    return count


# ===========================================================================
# Tradie SMS commands
# ===========================================================================

MUTE_HELP = (
    "Commands:\n"
    "MUTE +614xxxxxxxx — silence Joe for that customer for 24h\n"
    "UNMUTE +614xxxxxxxx — re-enable Joe immediately"
)


def handle_tradie_command(tradie: dict, command_body: str) -> str:
    business = tradie.get("business_name") or "the business"
    parts = command_body.strip().split(maxsplit=1)
    if not parts:
        return MUTE_HELP

    verb = parts[0].upper()
    arg = _normalise_phone(parts[1]) if len(parts) > 1 else ""

    if verb == "MUTE":
        if not arg:
            return f"MUTE needs a customer number, e.g. MUTE +61402585413\n\n{MUTE_HELP}"
        expires = add_mute(business, arg)
        if expires is None:
            return f"Sorry, couldn't apply the mute for {arg}. Try again."
        return (f"Joe muted for {arg} until {expires.isoformat(timespec='minutes')} UTC. "
                f"Their messages will be forwarded to you. Reply UNMUTE {arg} to re-enable.")

    if verb == "UNMUTE":
        if not arg:
            return f"UNMUTE needs a customer number, e.g. UNMUTE +61402585413\n\n{MUTE_HELP}"
        n = expire_mutes(business, arg)
        if n == 0:
            return f"No active mute found for {arg}."
        return f"Joe re-enabled for {arg}. Auto-replies will resume."

    return MUTE_HELP


# ===========================================================================
# Outbound SMS helpers
# ===========================================================================

def _send_sms(from_number: str, to_number: str, body: str) -> bool:
    """Generic outbound SMS via Twilio. Best-effort."""
    if twilio_client is None:
        log.error("Cannot send SMS: Twilio client not initialised")
        return False
    try:
        twilio_client.messages.create(body=body, from_=from_number, to=to_number)
        log.info("SMS sent from %s to %s", from_number, to_number)
        return True
    except Exception as exc:
        log.exception("Failed to send SMS: %s", exc)
        return False


def send_to_owner(tradie: dict, body: str) -> bool:
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    if not owner or not twilio_from:
        log.error("Cannot SMS owner: missing owner_mobile or phone_number")
        return False
    return _send_sms(twilio_from, owner, body)


def send_urgent_alert(tradie: dict, customer_number: str, customer_message: str, joe_reply: str, channel: str = "sms") -> None:
    biz = tradie.get("business_name") or "the business"
    body = (
        f"URGENT ({channel.upper()}) — {biz}\n"
        f"Customer: {customer_number}\n"
        f"Said: {customer_message}\n"
        f"Joe replied: {joe_reply}"
    )
    send_to_owner(tradie, body)


def send_voice_summary(tradie: dict, customer_number: str, transcript_lines: list[str]) -> None:
    biz = tradie.get("business_name") or "the business"
    transcript = "\n".join(transcript_lines)
    body = f"Voice call summary — {biz}\nFrom: {customer_number}\n\n{transcript}"
    send_to_owner(tradie, body)


def send_caller_handoff(tradie: dict, caller_number: str) -> None:
    """After a non-urgent voice call ends, prompt the caller via SMS to text
    their details. The caller will receive the SMS from the same Twilio
    number they just rang, so replying continues naturally with Joe-SMS."""
    biz = tradie.get("business_name") or "the business"
    twilio_from = _normalise_phone(tradie.get("phone_number", ""))
    if not twilio_from or not caller_number:
        log.error("Cannot send caller handoff: missing phone_number or caller_number")
        return
    body = (
        f"Hi, it's {ASSISTANT_NAME} from {biz}. "
        f"Please reply to this message with your name, suburb, and a brief "
        f"description of the job. The tradie will give you a call back within "
        f"the hour."
    )
    _send_sms(twilio_from, _normalise_phone(caller_number), body)


def send_muted_forward(tradie: dict, customer_number: str, customer_message: str) -> None:
    biz = tradie.get("business_name") or "the business"
    body = f"[Muted — {biz}]\nFrom: {customer_number}\n{customer_message}"
    send_to_owner(tradie, body)


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

def generate_reply(tradie: dict, user_message: str, history: list[dict], channel: str) -> str:
    business = tradie.get("business_name") or "the business"
    fallback = f"Hi, thanks for messaging {business}. We'll get back to you shortly."
    if channel == "voice":
        fallback = "Sorry, I'm having a bit of trouble. The tradie will call you back shortly."

    if anthropic_client is None:
        log.error("Anthropic client not initialised; using fallback reply")
        return fallback

    system_prompt = _build_system_prompt(tradie, channel)
    messages = history + [{"role": "user", "content": user_message}]

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120 if channel == "voice" else 200,
            system=system_prompt,
            messages=messages,
        )
        text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        reply = "".join(text_parts).strip()
        log.info("Claude %s reply (%d chars): %r", channel, len(reply), reply)
        return reply or fallback
    except Exception as exc:
        log.exception("Anthropic API call failed: %s", exc)
        return fallback


# ===========================================================================
# TwiML builders
# ===========================================================================

def _twiml_message(text: str) -> Response:
    twiml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{xml_escape(text)}</Message></Response>"
    )
    return Response(twiml, mimetype="application/xml")


def _twiml_empty() -> Response:
    twiml = "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"
    return Response(twiml, mimetype="application/xml")


def _voice_say(text: str) -> str:
    return (
        f'<Say voice="{xml_escape(VOICE_NAME)}" language="{xml_escape(VOICE_LANG)}">'
        f'{xml_escape(text)}</Say>'
    )


def _voice_gather(action: str, prompt: Optional[str] = None) -> str:
    inner = _voice_say(prompt) if prompt else ""
    return (
        f'<Gather input="speech" action="{xml_escape(action)}" method="POST" '
        f'speechTimeout="{xml_escape(SPEECH_TIMEOUT_SECONDS)}" '
        f'language="{xml_escape(VOICE_LANG)}" '
        f'speechModel="experimental_conversations">{inner}</Gather>'
    )


def _twiml_voice(*elements: str) -> Response:
    body = "".join(elements)
    twiml = f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"
    return Response(twiml, mimetype="application/xml")


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/", methods=["GET"])
def health() -> str:
    return "Tradie Receptionist v0.8.3 - alive (randomised handoff delay)"


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
    channel = request.args.get("channel", "sms")
    if channel not in ("sms", "voice"):
        channel = "sms"
    if not to:
        return Response("Pass ?to=+61...&channel=sms|voice", mimetype="text/plain")
    tradie = find_tradie(to)
    if not tradie:
        return Response(f"No tradie matched for {to}", mimetype="text/plain")
    return Response(_build_system_prompt(tradie, channel), mimetype="text/plain")


@app.route("/sms", methods=["POST"])
def sms_webhook() -> Response:
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    sid = request.form.get("MessageSid", "")
    body = request.form.get("Body", "") or ""
    log.info(
        "Inbound SMS | From=%s To=%s Sid=%s Body=%r",
        from_number, to_number, sid, body,
    )

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie found in Client tab for To=%s", to_number)
        return _twiml_message("Sorry, this number isn't configured yet.")

    business = tradie.get("business_name", "your tradie")
    log.info("Tradie matched: %s (%s)", business, to_number)

    sender = _normalise_phone(from_number)
    owner = _normalise_phone(tradie.get("owner_mobile", ""))
    body_stripped = body.strip()
    body_upper = body_stripped.upper()
    if sender == owner and (body_upper.startswith("MUTE") or body_upper.startswith("UNMUTE")):
        log.info("Tradie command from owner: %r", body_stripped)
        confirmation = handle_tradie_command(tradie, body_stripped)
        return _twiml_message(confirmation)

    if is_muted(business, from_number):
        log.info("Customer %s is muted for %s; forwarding only", from_number, business)
        send_muted_forward(tradie, from_number, body)
        log_conversation(business, from_number, to_number, body, "", False)
        return _twiml_empty()

    history = get_conversation_history(from_number, to_number)
    raw_reply = generate_reply(tradie, body, history, channel="sms")

    is_urgent = URGENT_TAG in raw_reply
    clean_reply = raw_reply.replace(URGENT_TAG, "").replace(END_TAG, "").strip()

    log_conversation(business, from_number, to_number, body, clean_reply, is_urgent)

    if is_urgent:
        send_urgent_alert(tradie, from_number, body, clean_reply, channel="sms")

    return _twiml_message(clean_reply)


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

@app.route("/voice", methods=["POST"])
def voice_incoming() -> Response:
    call_sid = request.form.get("CallSid", "")
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    log.info("Inbound CALL | CallSid=%s From=%s To=%s", call_sid, from_number, to_number)

    tradie = find_tradie(to_number)
    if not tradie:
        log.warning("No tradie for inbound call To=%s", to_number)
        return _twiml_voice(
            _voice_say("Sorry, this number isn't configured yet. Please try again later."),
            "<Hangup/>",
        )

    business = tradie.get("business_name", "the business")

    with _voice_state_lock:
        _voice_state[call_sid] = {
            "tradie": tradie,
            "from": from_number,
            "to": to_number,
            "history": [],
            "transcript_lines": [],
            "turn_count": 0,
            "ended": False,
        }

    greeting = (
        f"G'day, you've reached {business}. "
        f"I'm {ASSISTANT_NAME}. What can I help you with?"
    )
    return _twiml_voice(
        _voice_gather(action="/voice/turn", prompt=greeting),
        _voice_say("Sorry, I didn't catch that. The tradie will call you back."),
        "<Hangup/>",
    )


@app.route("/voice/turn", methods=["POST"])
def voice_turn() -> Response:
    call_sid = request.form.get("CallSid", "")
    speech = (request.form.get("SpeechResult", "") or "").strip()
    log.info("Voice turn | CallSid=%s SpeechResult=%r", call_sid, speech)

    with _voice_state_lock:
        state = _voice_state.get(call_sid)

    if not state:
        log.warning("No state for CallSid=%s; ending call", call_sid)
        return _twiml_voice(
            _voice_say("Sorry, something went wrong. Please call back."),
            "<Hangup/>",
        )

    tradie = state["tradie"]
    state["turn_count"] += 1

    if not speech:
        if state["turn_count"] < 2:
            return _twiml_voice(
                _voice_gather(action="/voice/turn",
                              prompt="Sorry, I didn't catch that. Could you say that again?"),
                _voice_say("Right, I'll have the tradie call you back. Thanks for ringing."),
                "<Hangup/>",
            )
        else:
            _finalise_call(call_sid, state, ended_reason="silence")
            return _twiml_voice(
                _voice_say("No worries, I'll have the tradie call you back. Thanks for ringing."),
                "<Hangup/>",
            )

    state["transcript_lines"].append(f"Customer: {speech}")
    raw_reply = generate_reply(tradie, speech, state["history"], channel="voice")

    is_urgent = URGENT_TAG in raw_reply
    is_end = END_TAG in raw_reply
    clean_reply = raw_reply.replace(URGENT_TAG, "").replace(END_TAG, "").strip()

    state["transcript_lines"].append(f"Joe: {clean_reply}")
    state["history"].append({"role": "user", "content": speech})
    state["history"].append({"role": "assistant", "content": clean_reply})

    if is_urgent:
        log.info("URGENT detected on call %s — attempting transfer", call_sid)
        owner = _normalise_phone(tradie.get("owner_mobile", ""))
        send_urgent_alert(tradie, state["from"], speech, clean_reply, channel="voice")
        _finalise_call(call_sid, state, ended_reason="urgent_transfer")

        if owner:
            return _twiml_voice(
                _voice_say(clean_reply),
                _voice_say("Putting you through to the tradie now. Hold on."),
                f'<Dial timeout="20" callerId="{xml_escape(_normalise_phone(state["to"]))}">'
                f'{xml_escape(owner)}</Dial>',
                _voice_say("They didn't pick up just then, but they've been alerted by text and will call you straight back."),
                "<Hangup/>",
            )
        else:
            return _twiml_voice(
                _voice_say(clean_reply),
                _voice_say("I've alerted the tradie and they'll call you straight back."),
                "<Hangup/>",
            )

    if is_end or state["turn_count"] >= VOICE_TURN_LIMIT:
        _finalise_call(call_sid, state, ended_reason=("end_tag" if is_end else "turn_limit"))
        return _twiml_voice(
            _voice_say(clean_reply),
            "<Hangup/>",
        )

    return _twiml_voice(
        _voice_gather(action="/voice/turn", prompt=clean_reply),
        _voice_say("Sorry, I didn't catch that. The tradie will call you back."),
        "<Hangup/>",
    )


@app.route("/voice/status", methods=["POST"])
def voice_status() -> Response:
    call_sid = request.form.get("CallSid", "")
    call_status = request.form.get("CallStatus", "")
    log.info("Voice status | CallSid=%s Status=%s", call_sid, call_status)

    with _voice_state_lock:
        state = _voice_state.get(call_sid)

    if state and not state.get("ended"):
        _finalise_call(call_sid, state, ended_reason=f"twilio_status:{call_status}")

    return _twiml_empty()


def _finalise_call(call_sid: str, state: dict, ended_reason: str) -> None:
    if state.get("ended"):
        return
    state["ended"] = True

    tradie = state["tradie"]
    business = tradie.get("business_name", "the business")
    transcript_lines = state["transcript_lines"]
    transcript = "\n".join(transcript_lines)

    log.info("Finalising call %s (reason=%s, turns=%d)",
             call_sid, ended_reason, state["turn_count"])

    if transcript_lines:
        log_conversation(
            business_name=business,
            from_number=state["from"],
            to_number=state["to"],
            message=f"[VOICE CALL]\n{transcript}",
            reply=f"[ended: {ended_reason}]",
            is_urgent=(ended_reason == "urgent_transfer"),
        )
        if ended_reason != "urgent_transfer":
            send_voice_summary(tradie, state["from"], transcript_lines)
            # Auto-SMS the caller asking for details, but delay it (random
            # range) so it feels like a real person sent the text — not a bot
            # firing instantly. Skips if call was urgent (urgent path transfers
            # live).
            delay = random.randint(
                CALLER_HANDOFF_DELAY_MIN_SECONDS,
                CALLER_HANDOFF_DELAY_MAX_SECONDS,
            )
            Timer(
                delay,
                send_caller_handoff,
                args=(tradie, state["from"]),
            ).start()
            log.info(
                "Caller handoff SMS scheduled in %ds for %s",
                delay, state["from"],
            )

    with _voice_state_lock:
        _voice_state.pop(call_sid, None)
